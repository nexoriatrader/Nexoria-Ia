"""NEXORA IA. Official Deriv DEMO orders and read-only AutoPilot research."""
import asyncio, json, logging, os, secrets, re, time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import websockets
from fastapi import FastAPI, HTTPException, Cookie, Request
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy import select, delete, func

from . import db
from .access import (access_control_enabled, access_status, start_trial_on_first_robot_start,
                     register_first_login, referral_report, utc)
from .demo import (DemoTradingError, DemoPreBuyDeadlineExceeded, demo_account_balance,
                   demo_contract_status, demo_buy, demo_proposal,
                   prepare_demo_buy_socket)
from .demo_ledger import OrderGuardError, reserve, public_order, confirmed, unknown, settle
from .autopilot import analyze, quote_assessment
from .engine import Engine, Settings
from .market import MarketError, discover_symbols, discover_contracts, indicative_proposal
from .strategy_lab import run_lab
from .single_asset_study import run_single_asset_study
from .asset_scanner import scan_assets
from .trade_selector_v2 import (select_demo_signal, last_scan_report, has_pending_signals,
                                active_market, catalogue, supported, fetch_closed_timeframe,
                                SIGNAL_PROFILE)
from .progression import next_entry, ProgressionHalt, NextEntry
from .security import CLIENT_ID, REDIRECT, TOKEN_URL, build_authorize_url, make_login, make_session_id, hashsecret, encrypt, decrypt, cookie_options

log=logging.getLogger('nexora')
log.setLevel(logging.INFO)  # Explicitly emit low-volume scanner diagnostics on Render.
VERSION='0.8.0'
PUBLIC_WS='wss://api.derivws.com/trading/v1/options/ws/public'
SYMBOL=os.getenv('NEXORA_SYMBOL','1HZ100V')
# Per-user runtime states are separate and intentionally temporary.
runtimes: dict[int, Engine] = {}
market_task = None
reconcile_task = None
autopilot_task = None
market_selfcheck_task = None
# Runtime state is volatile; explicit DEMO resume intent is persisted separately.
autopilot_enabled: set[int] = set()
autopilot_last_attempt: dict[int,float] = {}
v2_last_scan: dict[int,float] = {}
v2_gate_last_log: dict[tuple[int,str],float] = {}
autopilot_reasons: dict[int,str] = {}
autopilot_lock = asyncio.Lock()
market_last_tick = 0.0
market_connected = False
recent_prices=[]
last_market_reason='Aguardando conexão'
lock=asyncio.Lock()
reconcile_lock=asyncio.Lock()

def log_v2_gate(uid:int, gate:str, detail:str=''):
    """Rate-limit internal scanner gate diagnostics, without account secrets."""
    now=asyncio.get_running_loop().time()
    key=(uid,gate)
    if now-v2_gate_last_log.get(key,-1e12)>=60:
        v2_gate_last_log[key]=now
        log.warning('AutoPilot V2.2 gate: %s%s',gate,(' '+detail) if detail else '')


async def public_market_selfcheck_once():
    """One read-only public API smoke test per server boot; never buys."""
    await asyncio.sleep(6)
    market=active_market()
    expiry_minutes=5 if market=='volatility' else 15
    candle_seconds=300 if market=='volatility' else 900
    try:
        items=await asyncio.wait_for(catalogue(market),timeout=20)
        log.warning('V2.3 selfcheck: market=%s expiry=%sm catalogue=%s',market,expiry_minutes,len(items))
        if not items:
            log.warning('V2.2 selfcheck: empty catalogue; unable to inspect candles')
            return
        # Bounded: at most three Deriv-provided assets and no trading action.
        for item in items[:3]:
            symbol=item['symbol']
            try:
                ok=await asyncio.wait_for(supported(symbol,expiry_minutes),timeout=18)
                if not ok:
                    log.warning('V2.3 selfcheck: symbol=%s expiry=%smin CALL_PUT=unavailable',symbol,expiry_minutes)
                    continue
                snapshot=datetime.now(timezone.utc).timestamp()
                h1,m15=await asyncio.wait_for(asyncio.gather(
                    fetch_closed_timeframe(symbol,3600,105,now=snapshot),
                    fetch_closed_timeframe(symbol,candle_seconds,115,now=snapshot)),timeout=30)
                log.warning('V2.3 selfcheck: symbol=%s expiry=%smin CALL_PUT=available H1_candles=%s signal_granularity=%ss candles=%s',
                            symbol,expiry_minutes,len(h1),candle_seconds,len(m15))
                return
            except (MarketError,TimeoutError,OSError,ValueError) as exc:
                log.warning('V2.2 selfcheck: symbol=%s data_error=%s',symbol,type(exc).__name__)
        log.warning('V2.2 selfcheck: no successfully validated H1/M15 asset in sample')
    except (MarketError,TimeoutError,OSError,ValueError) as exc:
        log.warning('V2.2 selfcheck: catalogue_error=%s',type(exc).__name__)


def rt_for(user):
    engine=runtimes.get(user.id)
    if not engine:
        engine=Engine()
        engine.symbol=SYMBOL
        engine.settings=Settings.parse({'stake':user.stake,'martingale':user.martingale,'multiplier':user.multiplier,
            'stop_win':user.stop_win,'stop_loss':user.stop_loss})
        engine.connected=market_connected
        engine.prices.extend(recent_prices[-200:])
        engine.source='Deriv público (análise de mercado)'
        runtimes[user.id]=engine
    return engine

def active_session(request:Request, sid):
    if not sid: raise HTTPException(401,'Faça login com Deriv')
    with db.SessionLocal() as s:
        obj=s.get(db.Session,hashsecret(sid))
        expiry=obj.expires_at.replace(tzinfo=timezone.utc) if obj and obj.expires_at.tzinfo is None else (obj.expires_at if obj else None)
        if not obj or expiry<=db.utcnow(): raise HTTPException(401,'Sessão expirada; conecte novamente')
        user=s.get(db.User,obj.user_id)
        if not user: raise HTTPException(401,'Usuário não encontrado')
        return user, decrypt(obj.token_encrypted)

def active_user(request:Request, sid):
    return active_session(request, sid)[0]

async def feed():
    global market_connected, last_market_reason, recent_prices, market_last_tick
    delay=2
    while True:
        try:
            async with websockets.connect(PUBLIC_WS,ping_interval=20,ping_timeout=20) as ws:
                await ws.send(json.dumps({'ticks_history':SYMBOL,'count':100,'end':'latest','style':'ticks'}))
                await ws.send(json.dumps({'ticks':SYMBOL,'subscribe':1}))
                async with lock:
                    market_connected=True
                    last_market_reason='Cotações públicas conectadas'
                    for engine in runtimes.values():
                        engine.connected=True;engine.prices.clear()
                    recent_prices=[]
                delay=2
                async for raw in ws:
                    m=json.loads(raw)
                    if m.get('error'):raise RuntimeError(str(m['error'].get('message','Erro da Deriv')))
                    if m.get('msg_type')=='history':
                        ps=[float(p) for p in m.get('history',{}).get('prices',[])]
                        async with lock:
                            recent_prices=(recent_prices+ps)[-200:]
                            for engine in runtimes.values():engine.prices.extend(ps)
                    elif m.get('msg_type')=='tick':
                        p=float(m['tick']['quote'])
                        market_last_tick=asyncio.get_running_loop().time()
                        async with lock:
                            recent_prices=(recent_prices+[p])[-200:]
                            for uid,engine in list(runtimes.items()):
                                engine.prices.append(p)
                                # Public ticks are used for research only; no local trades.
        except asyncio.CancelledError: raise
        except Exception as e:
            # Include safe close status for diagnosis; never log authentication or tokens.
            close_info = ''
            if isinstance(e, websockets.exceptions.ConnectionClosed):
                frame = getattr(e, 'rcvd', None)
                close_info = ' close_code=' + str(getattr(frame, 'code', 'unknown'))
            log.warning('Public feed unavailable: %s%s',type(e).__name__,close_info)
            async with lock:
                market_connected=False;last_market_reason='Conexão interrompida'
                for engine in runtimes.values():
                    engine.connected=False
                    engine.last_reason='Conexão de cotações interrompida'
            await asyncio.sleep(delay)
            delay=min(delay*2,30)


def v2_enabled():
    return os.getenv('NEXORA_V2_ENABLED','false').lower()=='true'


def autopilot_allowed():
    return (os.getenv('NEXORA_AUTOPILOT_DEMO_ENABLED','false').lower()=='true'
            and os.getenv('NEXORA_DEMO_BUY_ENABLED','false').lower()=='true')


def autopilot_entry_plan(sess, user, orders):
    """Read immutable official ledger results since this run was armed."""
    plan=sess.get(db.RobotPlan,user.id)
    cfg=sess.get(db.RobotCycleParams,user.id)
    run=sess.get(db.RobotRun,user.id)
    if not run:
        raise ProgressionHalt('Reative o robô para iniciar uma nova sequência')
    previous=[o for o in sorted(orders,key=lambda x:x.id) if o.id>run.after_order_id]
    attribution_ids=[o.id for o in previous]
    attributions=sess.scalars(select(db.AutoPilotAttribution).where(
        db.AutoPilotAttribution.order_id.in_(attribution_ids))).all() if attribution_ids else []
    attrs={a.order_id:a for a in attributions}
    if len(attrs)!=len(previous):
        raise ProgressionHalt('Existem contratos fora do histórico automático desta sequência')
    rows=[{'id':o.id,'state':o.state,'pnl':o.pnl,'direction':o.direction,
           'symbol':o.symbol,'stake':o.stake,'strategy':attrs[o.id].strategy}
          for o in previous]
    cycles=(plan.cycles_limit if plan else 0) or 0
    entry=next_entry(base=user.stake,gale_enabled=bool(plan and plan.requested_martingale),
        gales=plan.gale_count if plan and plan.requested_martingale else 0,
        gale_multiplier=plan.requested_multiplier if plan else '2',
        cycles=cycles or None,cycles_enabled=cycles>0,
        cycle_multiplier=cfg.multiplier if cfg else '2',orders=rows)
    last=previous[-1] if previous else None
    return entry,last



def disarm_autopilot(uid:int, reason:str):
    """Revoke persistent opt-in on a safety stop."""
    autopilot_enabled.discard(uid)
    with db.SessionLocal() as sess:
        consent=sess.get(db.AutoPilotResumeConsent,uid)
        if consent:
            sess.delete(consent)
            sess.commit()
    autopilot_reasons[uid]=reason


def restore_demo_autopilot_consent():
    """Boot-time, DEMO-only restoration after local fail-closed checks."""
    if not autopilot_allowed():
        return
    with db.SessionLocal() as sess:
        ids=sess.scalars(select(db.AutoPilotResumeConsent.user_id)).all()
        for uid in ids:
            user=sess.get(db.User,uid)
            run=sess.get(db.RobotRun,uid)
            orders=sess.scalars(select(db.DemoOrder).where(db.DemoOrder.user_id==uid)).all()
            sessions=sess.scalars(select(db.Session).where(
                db.Session.user_id==uid).order_by(db.Session.created_at.desc())).all()
            valid_auth=False
            for auth in sessions:
                expiry=auth.expires_at
                if expiry.tzinfo is None: expiry=expiry.replace(tzinfo=timezone.utc)
                if expiry<=db.utcnow()+timedelta(minutes=5): continue
                try:
                    decrypt(auth.token_encrypted)
                    valid_auth=True
                    break
                except Exception:
                    continue
            reason=None
            if not access_status(sess,uid)['allowed']:
                reason='Acesso ao robô expirado ou ainda não ativado'
            if reason is None and (not user or not run or user.selected_mode!='demo' or user.martingale):
                reason='Retomada bloqueada: configuração ou conta DEMO inválida'
            elif reason is None and not valid_auth:
                reason='Retomada bloqueada: sessão Deriv expirada ou indisponível'
            elif reason is None and any(o.state in ('PENDING','UNKNOWN') for o in orders):
                reason='Retomada bloqueada: contrato pendente ou incerto; confira os resultados'
            elif reason is None:
                boundary=latest_risk_session(sess,uid)
                reason=autopilot_start_blocker(
                    user,orders,after_order_id=boundary.after_order_id if boundary else 0)
                if reason is None:
                    try:
                        batch=sess.get(db.PilotValidationBatch,uid)
                        if batch:
                            # V2 progression is independent per active symbol.
                            v2_symbol_recovery(sess,user,orders,
                                sess.get(db.RobotPlan,uid),
                                sess.get(db.RobotCycleParams,uid),batch)
                        else:
                            autopilot_entry_plan(sess,user,orders)
                    except ProgressionHalt as exc:
                        if str(exc)!='Contrato anterior não liquidado; aguardando reconciliação':
                            reason=str(exc)
            if reason:
                consent=sess.get(db.AutoPilotResumeConsent,uid)
                if consent:sess.delete(consent)
                autopilot_reasons[uid]=reason
                log.warning('DEMO AutoPilot resume blocked: user=%s',uid)
            else:
                autopilot_enabled.add(uid)
                autopilot_last_attempt.pop(uid,None)
                autopilot_reasons[uid]='AutoPilot DEMO restaurado; aguardando dados e liquidação oficial'
                log.info('DEMO AutoPilot resumed: user=%s',uid)
        sess.commit()


def v2_symbol_recovery(sess,user,orders,plan,cycle_cfg,batch):
    """Pick an owed Martingale on its ORIGINAL symbol, after broker settlement.

    Per-symbol order streams must not be combined: each symbol has its own
    sequential progression and an active trade always blocks its own symbol.
    """
    if not batch:
        return None,None
    run=sess.get(db.RobotRun,user.id)
    if not run:
        raise ProgressionHalt('Reative o AutoPilot para iniciar uma sequência DEMO')
    # New manual activation resets progression, not accumulated V2 evaluation.
    after=max(batch.after_order_id,run.after_order_id)
    # SQL rows have no guaranteed order unless ORDER BY is explicit. The
    # progression validator requires monotonically increasing broker ledger IDs.
    # Sort a new view; never rewrite historical rows or their outcomes.
    # A timing-expired V2.4 attempt was cancelled BEFORE broker transmission.
    # Keep it in the audit ledger but never treat it as a trade or loss.
    historical=sorted((o for o in orders if o.id>after and o.state!='CANCELLED'),
                      key=lambda o:o.id)
    if not historical:
        return None,None
    ids=[o.id for o in historical]
    attributes={a.order_id:a for a in sess.scalars(select(db.AutoPilotAttribution).where(
        db.AutoPilotAttribution.user_id==user.id,
        db.AutoPilotAttribution.order_id.in_(ids))).all()}
    symbol_groups={}
    for order in historical:
        if order.id not in attributes:
            # Manual operations cannot be blended into progression.
            raise ProgressionHalt('Contrato sem atribuição automática na avaliação 2.0')
        symbol_groups.setdefault(order.symbol,[]).append(order)
    next_retries=[]
    for symbol,group in symbol_groups.items():
        if any(o.state in ('PENDING','UNKNOWN','CONFIRMED') for o in group):
            continue
        rows=[{'id':o.id,'state':o.state,'pnl':o.pnl,'direction':o.direction,
               'symbol':o.symbol,'stake':o.stake,
               'strategy':attributes[o.id].strategy} for o in group]
        try:
            entry=next_entry(base=user.stake,
                gale_enabled=bool(plan and plan.requested_martingale),
                gales=plan.gale_count if plan and plan.requested_martingale else 0,
                gale_multiplier=plan.requested_multiplier if plan else '2',
                cycles=(plan.cycles_limit if plan else None),
                cycles_enabled=bool(plan and plan.cycles_limit),
                cycle_multiplier=cycle_cfg.multiplier if cycle_cfg else '2',orders=rows)
        except ProgressionHalt as exc:
            if 'Quantidade de ciclos concluída' in str(exc):
                continue
            raise
        if entry.retry:
            next_retries.append((group[-1].id,entry,group[-1]))
    if not next_retries:
        return None,None
    _,entry,last=max(next_retries,key=lambda pair:pair[0])
    return entry,last


def v24_recovery_timing(settled_at, *, now=None, bar_seconds=300):
    """Next M5 (Volatility) or M15 (Forex) gate after verified settlement.

    Returns (target_open_epoch, ready_now, warmup_now).
    Never permit the candle already underway at the settlement timestamp.
    """
    if bar_seconds not in (300,900):
        raise ValueError('Timeframe de recuperação inválido')
    current=time.time() if now is None else float(now)
    if settled_at.tzinfo is None:
        settled_at=settled_at.replace(tzinfo=timezone.utc)
    settled_ts=settled_at.timestamp()
    target=int(current//bar_seconds)*bar_seconds
    if current>target+3:
        target+=bar_seconds
    # A recovery always follows, never precedes, confirmed settlement.
    if target<=settled_ts:
        target=(int(settled_ts//bar_seconds)+1)*bar_seconds
    remaining=target-current
    ready=target<=current<=target+3 and settled_ts<target
    warmup=10<=remaining<=45 and settled_ts<target
    return target,ready,warmup


def m1_recovery_window_open(settled_at, now=None):
    """Allow recovery only in first 8s of a strictly later M1 candle.

    This guards against a *late* request, not a broker execution-time guarantee.
    """
    current=now or datetime.now(timezone.utc)
    if settled_at.tzinfo is None:
        settled_at=settled_at.replace(tzinfo=timezone.utc)
    current_ts=current.timestamp()
    candle_start=int(current_ts)//60*60
    next_after_settlement=(int(settled_at.timestamp())//60+1)*60
    return candle_start>=next_after_settlement and 0<=current_ts-candle_start<=8


async def autopilot_once():
    """DEMO order progression uses only settled, verified broker contracts."""
    if not autopilot_enabled:
        return
    now=asyncio.get_running_loop().time()
    async with lock:
        connected=market_connected and market_last_tick>0 and now-market_last_tick<35
        prices=list(recent_prices)
    # V2.2 uses independent H1/M15 candles for each catalogue symbol;
    # only legacy V1 requires the separate realtime tick feed.
    if not connected:
        with db.SessionLocal() as sess:
            live_v2={v for v in autopilot_enabled
                     if sess.get(db.PilotValidationBatch,v) is not None}
        for uid in tuple(autopilot_enabled):
            if uid not in live_v2:
                autopilot_reasons[uid]='Aguardando cotações recentes da Deriv'
        if not live_v2:
            return
    research=analyze(prices) if connected else {'candidates':[],'reason':'Cotações indisponíveis'}
    candidates=research['candidates']
    for uid in tuple(autopilot_enabled):
        if uid not in autopilot_enabled:
            continue
        async with autopilot_lock:
            if uid not in autopilot_enabled or not autopilot_allowed():
                continue
            with db.SessionLocal() as sess:
                user=sess.get(db.User,uid)
                if not user or user.selected_mode!='demo':
                    disarm_autopilot(uid,'Conta de treinamento indisponível; robô desligado')
                    continue
                if not access_status(sess,uid)['allowed']:
                    disarm_autopilot(uid,'Acesso do robô expirou: novas compras bloqueadas; contratos existentes serão reconciliados')
                    continue
                auths=sess.scalars(select(db.Session).where(db.Session.user_id==uid)
                                 .order_by(db.Session.created_at.desc())).all()
                token=None
                for auth in auths:
                    expiry=auth.expires_at
                    if expiry.tzinfo is None:
                        expiry=expiry.replace(tzinfo=timezone.utc)
                    if expiry>db.utcnow():
                        try:
                            token=decrypt(auth.token_encrypted)
                            break
                        except Exception:
                            continue
                if not token:
                    disarm_autopilot(uid,'Sessão Deriv expirada; conecte novamente')
                    continue
                orders=sess.scalars(select(db.DemoOrder).where(db.DemoOrder.user_id==uid)).all()
                plan=sess.get(db.RobotPlan,uid)
                cycle_cfg=sess.get(db.RobotCycleParams,uid)
                batch=sess.get(db.PilotValidationBatch,uid)
                if not batch and not connected:
                    continue
                # The goal of 50 trades is informational, NOT an execution cap.
                # Risk stops and broker-uncertainty safeguards remain mandatory.
                try:
                    if batch:
                        active=[o for o in orders if o.state in ('PENDING','CONFIRMED','UNKNOWN')]
                        if any(o.state in ('PENDING','UNKNOWN') for o in active):
                            log_v2_gate(uid,'PENDING_OR_UNKNOWN_CONTRACT')
                            autopilot_reasons[uid]='Contrato DEMO incerto; aguardando conferência'
                            continue
                        entry,last=v2_symbol_recovery(sess,user,orders,plan,cycle_cfg,batch)
                        if entry is None:
                            entry=NextEntry(stake=Decimal(str(user.stake)),cycle=0,gale=0,retry=False)
                    else:
                        entry,last=autopilot_entry_plan(sess,user,orders)
                except ProgressionHalt as exc:
                    reason=str(exc)
                    # An unliquidated broker contract is normal during the
                    # contract lifetime. Stay armed, but NEVER send another buy
                    # until the reconciliation worker confirms settlement.
                    if reason=='Contrato anterior não liquidado; aguardando reconciliação':
                        autopilot_reasons[uid]='Aguardando liquidação oficial do contrato anterior na Deriv'
                        continue
                    disarm_autopilot(uid,reason)
                    continue
                if entry.retry:
                    # V2 recovery is not held by the 60s fresh-scan cooldown.
                    # It can start promptly AFTER verified official settlement.
                    # Legacy mode retains its original M1 candle rule.
                    if not batch and now-autopilot_last_attempt.get(uid,-1e12)<60:
                        continue
                    settlement=last.updated_at if last else None
                    if not settlement:
                        autopilot_reasons[uid]='Aguardando horário da liquidação oficial'
                        continue
                    if settlement.tzinfo is None:
                        settlement=settlement.replace(tzinfo=timezone.utc)
                    if not batch and not m1_recovery_window_open(settlement):
                        autopilot_reasons[uid]='Aguardando abertura de vela M1 para recuperação'
                        continue
                    if batch and now-autopilot_last_attempt.get(uid,-1e12)<5:
                        log_v2_gate(uid,'RECOVERY_COOLDOWN')
                        continue
                    # Warm the authenticated DEMO socket while there is time.
                    # Otherwise Martingale follows a cold OTP+WS connection and
                    # commonly misses the three-second M5 opening window.
                    if (batch and SIGNAL_PROFILE=='next_open_v24'
                        and last is not None
                        and last.duration_minutes in (5,15)
                        and (active_market()=='volatility' if last.duration_minutes==5
                             else active_market()=='forex')):
                        recovery_target,recovery_ready,should_warm=(
                            v24_recovery_timing(settlement,bar_seconds=last.duration_minutes*60))
                        if should_warm:
                            warm_start=time.monotonic()
                            warmed=await prepare_demo_buy_socket(token,user.deriv_account_id)
                            log_v2_gate(uid,'RECOVERY_WARMUP',
                                        'ready='+str(warmed)+
                                        ' latency_s=%.3f' % (time.monotonic()-warm_start))
                        if not recovery_ready:
                            autopilot_reasons[uid]='Recuperação aguardando próxima abertura M'+str(last.duration_minutes)+' após liquidação'
                            continue
                    if batch: log_v2_gate(uid,'RECOVERY_PENDING')
                    candidate={'strategy':entry.strategy or 'recuperacao',
                        'direction':entry.direction,
                        'symbol':entry.symbol or SYMBOL,
                        'minutes':last.duration_minutes if last else 5,
                        'reason':'Continuação controlada: ciclo '+str(entry.cycle)+
                                 ', martingale '+str(entry.gale)}
                else:
                    if not batch and now-autopilot_last_attempt.get(uid,-1e12)<60:
                        continue
                    if batch:
                        # Fresh confirmed signals from the same batch can be
                        # consumed on subsequent ticks without a new API scan.
                        if (now-v2_last_scan.get(uid,-1e12)<15
                            and not has_pending_signals(uid,filtered=batch.max_orders>=100 and batch.max_orders<200,
                                                         strategy_mode='pullback_v22' if batch.max_orders>=200 else None)):
                            log_v2_gate(uid,'SCAN_INTERVAL')
                            continue
                        if now-autopilot_last_attempt.get(uid,-1e12)<5:
                            log_v2_gate(uid,'ORDER_COOLDOWN')
                            continue
                        v2_last_scan[uid]=now
                        try:
                            candidate=await select_demo_signal(
                                exclude_symbols=[o.symbol for o in orders
                                                 if o.state in ('PENDING','CONFIRMED','UNKNOWN')],
                                user_key=uid,filtered=batch.max_orders>=100 and batch.max_orders<200,
                                strategy_mode='pullback_v22' if batch.max_orders>=200 else None)
                        except Exception as exc:
                            autopilot_reasons[uid]='Scanner 2.0 indisponível; aguardando reconexão'
                            log.warning('V2 scanner unavailable: %s',type(exc).__name__)
                            continue
                        if candidate is None:
                            state=last_scan_report()
                            status=state.get('status','UNKNOWN')
                            if status=='PREPARED_NEXT_OPEN' and SIGNAL_PROFILE=='next_open_v24':
                                # Warm connection while the signal is queued; never
                                # transmit any buy before the opening deadline.
                                warm_start=time.monotonic()
                                warmed=await prepare_demo_buy_socket(token,user.deriv_account_id)
                                log.warning('V2.4 Deriv warmup: ready=%s latency_s=%.3f',
                                            warmed,time.monotonic()-warm_start)
                            log_v2_gate(uid,'SCAN_'+status,'market='+str(state.get('market','?')))
                            if status=='OUTSIDE_TRADING_SESSION':
                                autopilot_reasons[uid]='AutoPilot 2.2: fora do horário de análise (05h às 14h, Brasília)'
                            elif status in ('CATALOG_UNAVAILABLE','NO_SUPPORTED_ASSETS_IN_CATALOG',
                                            'SCAN_TIMEOUT','MARKET_DATA_UNAVAILABLE'):
                                autopilot_reasons[uid]='Scanner 2.0 sem dados válidos: '+status
                            else:
                                if batch.max_orders>=200:
                                    current_market=state.get('market','forex')
                                    autopilot_reasons[uid]=(
                                        'AutoPilot 2.2: Volatility em análise; aguardando sinal confirmado'
                                        if current_market=='volatility' else
                                        'AutoPilot 2.2: Forex H1/M15 em análise; aguardando sinal confirmado')
                                else:
                                    autopilot_reasons[uid]='AutoPilot 2.0: sem rompimento ou tendência confirmados neste ciclo'
                            continue
                    else:
                        if not candidates:
                            autopilot_reasons[uid]='Buscando oportunidade: '+research['reason']
                            continue
                        previous=sess.scalars(select(db.AutoPilotAttribution.id).where(
                            db.AutoPilotAttribution.user_id==uid)).all()
                        candidate=candidates[len(previous)%len(candidates)]
                        candidate={**candidate,'symbol':SYMBOL,'minutes':5}
                from .demo_ledger import order_guard
                try:
                    boundary=latest_risk_session(sess,uid)
                    order_guard(user,orders,entry.stake,
                        after_order_id=boundary.after_order_id if boundary else 0,
                        parallel=bool(batch),symbol=candidate['symbol'])
                except OrderGuardError as exc:
                    msg=str(exc)
                    if 'Stop' in msg or 'Loss' in msg or 'Win' in msg or 'ultrapass' in msg:
                        disarm_autopilot(uid,msg)
                    continue
                if entry.retry and not batch and entry.symbol!=SYMBOL:
                    disarm_autopilot(uid,'O ativo anterior não está disponível para recuperação')
                    continue
                if batch and not entry.retry and candidate.get('strategy') not in ('h1_m5_next_open_v24','h1_m15_next_open_v24'):
                    # The v2.4 buy proposal itself retains payout validation.
                    # Fresh entries have two proposal checks. For recovery,
                    # skip this redundant preflight; the actual broker proposal
                    # is STILL checked for min payout immediately before buy.
                    try:
                        quote=await demo_proposal(token,user.deriv_account_id,
                            symbol=candidate['symbol'],contract_type=candidate['direction'],
                            stake=str(entry.stake),duration=candidate['minutes'])
                        ask=Decimal(str(quote['ask_price']))
                        payout=Decimal(str(quote['payout']))
                        if (not ask.is_finite() or not payout.is_finite()
                            or ask<=0 or ask>entry.stake or payout<ask*Decimal('1.50')):
                            autopilot_reasons[uid]='Proposta DEMO sem payout mínimo experimental de 1,50×'
                            log.info('V2 quote rejected: symbol=%s duration=%s reason=payout_or_ask',
                                     candidate['symbol'],candidate['minutes'])
                            continue
                    except (DemoTradingError,InvalidOperation,KeyError,ValueError,TypeError) as exc:
                        autopilot_reasons[uid]='Ativo ou duração sem proposta DEMO compatível; nova análise depois'
                        log.warning('V2 quote unavailable: symbol=%s duration=%s error_type=%s deriv_code=%s',
                                    candidate['symbol'],candidate['minutes'],type(exc).__name__,
                                    getattr(exc,'deriv_code','NOT_BROKER_REJECTION'))
                        continue
            if uid not in autopilot_enabled:
                continue
            deadline_for_buy=None
            timed_v24=(bool(batch) and SIGNAL_PROFILE=='next_open_v24'
                       and candidate['minutes'] in (5,15)
                       and active_market()==('volatility' if candidate['minutes']==5 else 'forex'))
            if timed_v24:
                dispatch_at=time.time()
                # Fresh signal uses its prepared boundary. Recoveries may only
                # send at a five-minute opening; never invent a fresh signal.
                target=(v24_recovery_timing(last.updated_at,now=dispatch_at,
                          bar_seconds=last.duration_minutes*60)[0]
                        if entry.retry and last is not None
                        else candidate.get('target_open_epoch'))
                if target is None:
                    log.warning('V2.4 missing prepared opening: symbol=%s',candidate['symbol'])
                    continue
                deadline_for_buy=target+3.0
                if not target<=dispatch_at<=deadline_for_buy:
                    if not entry.retry:
                        log.warning('V2.4 skipped late candidate: symbol=%s delta_s=%.3f',
                                    candidate['symbol'],dispatch_at-target)
                    autopilot_reasons[uid]=('Aguardando próxima abertura M'+str(candidate['minutes'])+' para recuperação'
                                           if entry.retry else
                                           'Sinal M'+str(candidate['minutes'])+' descartado: fora da abertura da vela')
                    continue
            autopilot_last_attempt[uid]=now
            key='auto-'+secrets.token_hex(16)
            autopilot_reasons[uid]='Enviando contrato de treinamento: '+str(entry.stake)+' USD'
            try:
                result=await perform_demo_buy(user,token,
                    DemoBuy(symbol=candidate['symbol'],direction=candidate['direction'],
                            minutes=candidate['minutes'],confirm='DEMO_ONLY',request_key=key),
                    attribution={'strategy':candidate['strategy'],'reason':candidate['reason'],
                                 'v2':bool(batch),
                                 'cycle_index':entry.cycle,'gale_index':entry.gale,
                                 'gale_multiplier':(plan.requested_multiplier if plan else '2.00'),
                                 'cycle_multiplier':(cycle_cfg.multiplier if cycle_cfg else '2.00')},
                    stake_override=entry.stake,
                    latest_buy_send_epoch=deadline_for_buy)
                autopilot_reasons[uid]='Contrato DEMO #'+str(result['id'])+' confirmado; aguardando liquidação'
            except HTTPException as exc:
                msg=str(exc.detail)
                autopilot_reasons[uid]=msg
                if exc.status_code in (401,403,502,503) or 'Stop' in msg:
                    disarm_autopilot(uid,msg)
            except Exception as exc:
                disarm_autopilot(uid,'Falha na execução; robô desligado para segurança')
                log.warning('AutoPilot execution halted: user=%s error=%s',uid,type(exc).__name__)


async def autopilot_loop():
    while True:
        try:
            await autopilot_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception('AutoPilot loop error')
        await asyncio.sleep(1)


async def reconcile_confirmed_orders_once():
    """Read official settlement for known contract IDs; never send broker buys.

    Include UNKNOWN only when Deriv's contract ID is already durably known.
    A contract without ID cannot be automatically reconciled safely.
    Prioritize recent contracts so older unresolved records cannot starve them.
    """
    with db.SessionLocal() as s:
        order_ids=s.scalars(select(db.DemoOrder.id).where(
            db.DemoOrder.state.in_(('CONFIRMED','UNKNOWN')),
            db.DemoOrder.contract_id.is_not(None))
            .order_by(db.DemoOrder.id.desc()).limit(100)).all()
    for order_id in order_ids:
        async with reconcile_lock:
            with db.SessionLocal() as s:
                order=s.get(db.DemoOrder,order_id)
                if not order or order.state not in ('CONFIRMED','UNKNOWN') or not order.contract_id:
                    continue
                user=s.get(db.User,order.user_id)
                if not user:
                    continue
                sessions=s.scalars(select(db.Session).where(db.Session.user_id==user.id)
                                   .order_by(db.Session.created_at.desc())).all()
                token=None
                for auth_session in sessions:
                    expiry=auth_session.expires_at
                    if expiry.tzinfo is None:
                        expiry=expiry.replace(tzinfo=timezone.utc)
                    if expiry<=db.utcnow():
                        continue
                    try:
                        token=decrypt(auth_session.token_encrypted)
                    except Exception as exc:
                        log.warning('Demo reconciliation session unavailable: %s',type(exc).__name__)
                        continue
                    break
                account_id=user.deriv_account_id
                contract_id=order.contract_id
            if not token:
                continue
            try:
                receipt=await demo_contract_status(token,account_id,contract_id)
                with db.SessionLocal() as s:
                    order=s.get(db.DemoOrder,order_id)
                    if not order or order.state not in ('CONFIRMED','UNKNOWN'):
                        continue
                    try:
                        settled=settle(s,order,receipt.get('contract',{}))
                    except OrderGuardError as exc:
                        log.warning('Demo reconciliation rejected: order=%s error=%s',order_id,type(exc).__name__)
                        continue
                    if settled:
                        log.info('Demo contract settlement recorded from Deriv: order=%s',order_id)
            except DemoTradingError as exc:
                log.warning('Demo reconciliation unavailable: order=%s error=%s',order_id,type(exc).__name__)


async def reconcile_confirmed_loop():
    while True:
        try:
            await reconcile_confirmed_orders_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning('Demo reconciliation loop failed: %s',type(exc).__name__)
        await asyncio.sleep(3)

@asynccontextmanager
async def lifespan(app):
    global market_task, reconcile_task, autopilot_task
    if os.getenv('NEXORA_PUBLIC_URL','').startswith('https://') and db.DB_URL.startswith('sqlite:'):
        raise RuntimeError('Não usar SQLite efêmero em implantação pública: configure DATABASE_URL de Postgres')
    db.init_db()
    restore_demo_autopilot_consent()
    market_task=asyncio.create_task(feed())
    reconcile_task=asyncio.create_task(reconcile_confirmed_loop())
    autopilot_task=asyncio.create_task(autopilot_loop())
    market_selfcheck_task=asyncio.create_task(public_market_selfcheck_once())
    yield
    autopilot_enabled.clear()
    tasks=[task for task in (market_task,reconcile_task,autopilot_task,market_selfcheck_task) if task]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks,return_exceptions=True)

app=FastAPI(title='NEXORA IA',version=VERSION,lifespan=lifespan)

@app.middleware('http')
async def headers(request:Request, call_next):
    # Same-origin browser calls; origin check on mutating requests protects session cookies from CSRF.
    if request.method in ('POST','PUT','PATCH','DELETE'):
        origin=request.headers.get('origin')
        if origin:
            expected=os.getenv('NEXORA_PUBLIC_URL','http://localhost:8000').rstrip('/')
            if origin.rstrip('/')!=expected:
                return JSONResponse({'detail':'Origem não autorizada'},403)
    response=await call_next(request)
    response.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer',
        'X-Frame-Options':'DENY','Content-Security-Policy':"default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'"})
    return response

@app.get('/')
async def index(): return FileResponse(Path(__file__).parent/'index.html')

@app.get('/i/{slug}')
async def short_referral_redirect(slug:str):
    """First-party shortener preserves the original OAuth attribution path."""
    if not re.fullmatch(r'[A-Za-z0-9_-]{8}',slug):
        raise HTTPException(404,'Convite não encontrado')
    with db.SessionLocal() as sess:
        link=sess.scalar(select(db.ReferralShortLink).where(db.ReferralShortLink.slug==slug))
        if not link:
            raise HTTPException(404,'Convite não encontrado')
        original=sess.get(db.ReferralLink,link.user_id)
        if not original:
            raise HTTPException(404,'Convite não encontrado')
        return RedirectResponse('/?ref='+original.code,status_code=302)


@app.get('/social-preview.png')
async def social_preview_png():
    # Preserve old social image URL while using the approved unmodified artwork.
    return RedirectResponse(
        'https://i.ibb.co/DfDX477p/social-preview-approved.png',
        status_code=302,headers={'Cache-Control':'public, max-age=300'}
    )


@app.get('/favicon.svg')
async def favicon_svg():
    return FileResponse(Path(__file__).parent/'static'/'favicon.svg',media_type='image/svg+xml')


@app.get('/brand/logo.png')
async def brand_logo(): return FileResponse(Path(__file__).parent/'static'/'logo-nexora.png', media_type='image/png')

@app.get('/brand/logo-web.png')
async def brand_logo_web(): return FileResponse(Path(__file__).parent/'static'/'logo-nexora-web.png', media_type='image/png')

@app.get('/brand/logo-transparent.png')
async def brand_logo_transparent(): return FileResponse(Path(__file__).parent/'static'/'logo-transparent.png', media_type='image/png')

@app.get('/brand/logo-transparent-web.png')
async def brand_logo_transparent_web(): return FileResponse(Path(__file__).parent/'static'/'logo-transparent-web.png', media_type='image/png')

@app.get('/brand/logo-top.png')
async def brand_logo_top(): return FileResponse(Path(__file__).parent/'static'/'logo-top.png', media_type='image/png')

@app.get('/brand/logo-nexora-v21.svg')
async def brand_logo_v21():
    return FileResponse(Path(__file__).parent/'static'/'logo-nexora-v21.svg',media_type='image/svg+xml')


@app.get('/brand/logo-nexora-clean.png')
async def brand_logo_nexora_clean(): return FileResponse(Path(__file__).parent/'static'/'logo-nexora-clean.png', media_type='image/png')

@app.get('/brand/emblems-nexora.png')
async def brand_emblems_nexora(): return FileResponse(Path(__file__).parent/'static'/'emblems-nexora.png', media_type='image/png')

@app.get('/brand/icon-market.svg')
async def brand_icon_market(): return FileResponse(Path(__file__).parent/'static'/'icon-market.svg', media_type='image/svg+xml')

@app.get('/brand/icon-ai.svg')
async def brand_icon_ai(): return FileResponse(Path(__file__).parent/'static'/'icon-ai.svg', media_type='image/svg+xml')

@app.get('/brand/icon-risk.svg')
async def brand_icon_risk(): return FileResponse(Path(__file__).parent/'static'/'icon-risk.svg', media_type='image/svg+xml')

@app.get('/brand/logo-top-display-transparent.png')
async def brand_logo_top_display_transparent(): return FileResponse(Path(__file__).parent/'static'/'logo-top-display-transparent.png', media_type='image/png')

@app.get('/brand/logo-top-display.jpg')
async def brand_logo_top_display(): return FileResponse(Path(__file__).parent/'static'/'logo-top-display.jpg', media_type='image/jpeg')

@app.get('/brand/logo-top.jpg')
async def brand_logo_top_jpg(): return FileResponse(Path(__file__).parent/'static'/'logo-top.jpg', media_type='image/jpeg')

@app.get('/brand/logo-top-1.png')
async def brand_logo_top_1_png(): return FileResponse(Path(__file__).parent/'static'/'logo-top-1.png', media_type='image/png')

@app.get('/brand/logo-top-2.png')
async def brand_logo_top_2_png(): return FileResponse(Path(__file__).parent/'static'/'logo-top-2.png', media_type='image/png')

@app.get('/brand/logo-top-3.png')
async def brand_logo_top_3_png(): return FileResponse(Path(__file__).parent/'static'/'logo-top-3.png', media_type='image/png')

@app.get('/brand/logo-top-4.png')
async def brand_logo_top_4_png(): return FileResponse(Path(__file__).parent/'static'/'logo-top-4.png', media_type='image/png')

@app.get('/brand/logo-top-5.png')
async def brand_logo_top_5_png(): return FileResponse(Path(__file__).parent/'static'/'logo-top-5.png', media_type='image/png')

@app.get('/brand/logo-top-6.png')
async def brand_logo_top_6_png(): return FileResponse(Path(__file__).parent/'static'/'logo-top-6.png', media_type='image/png')

@app.get('/brand/logo-top-7.png')
async def brand_logo_top_7_png(): return FileResponse(Path(__file__).parent/'static'/'logo-top-7.png', media_type='image/png')

@app.get('/brand/logo-top-8.png')
async def brand_logo_top_8_png(): return FileResponse(Path(__file__).parent/'static'/'logo-top-8.png', media_type='image/png')

@app.get('/brand/logo-top-1.jpg')
async def brand_logo_top_1(): return FileResponse(Path(__file__).parent/'static'/'logo-top-1.jpg', media_type='image/jpeg')

@app.get('/brand/logo-top-2.jpg')
async def brand_logo_top_2(): return FileResponse(Path(__file__).parent/'static'/'logo-top-2.jpg', media_type='image/jpeg')

@app.get('/brand/logo-top-3.jpg')
async def brand_logo_top_3(): return FileResponse(Path(__file__).parent/'static'/'logo-top-3.jpg', media_type='image/jpeg')

@app.get('/brand/logo-top-4.jpg')
async def brand_logo_top_4(): return FileResponse(Path(__file__).parent/'static'/'logo-top-4.jpg', media_type='image/jpeg')

@app.get('/brand/reference-bg.png')
async def brand_reference_bg(): return FileResponse(Path(__file__).parent/'static'/'reference-bg.png', media_type='image/png')

@app.get('/healthz')
async def health():
    enabled=os.getenv('NEXORA_DEMO_BUY_ENABLED','false').lower()=='true'
    return {'ok':True,'version':VERSION,'market_connected':market_connected,
            'execution':'DERIV_DEMO_ONLY','automatic_demo_orders_enabled':autopilot_allowed(),
            'manual_demo_purchase_enabled':enabled,'real_orders_enabled':False}

@app.get('/auth/deriv/start')
async def login_start(ref:str|None=None):
    state,verifier=make_login()
    if ref and not re.fullmatch(r'[A-Za-z0-9_-]{8,32}',ref):
        ref=None
    with db.SessionLocal() as s:
        # expiry handled at consumption; old flows can be pruned in maintenance
        s.add(db.LoginFlow(state_hash=hashsecret(state),verifier_encrypted=encrypt(verifier),referral_code=ref));s.commit()
    response=RedirectResponse(build_authorize_url(state,verifier),302)
    response.set_cookie('nexora_state',state,max_age=600,**cookie_options())
    return response

@app.get('/auth/deriv/callback')
async def callback(code:str|None=None,state:str|None=None,error:str|None=None,
                   nexora_state:str|None=Cookie(default=None)):
    if not state or not nexora_state or not secrets.compare_digest(state,nexora_state):
        raise HTTPException(400,'Estado OAuth inválido')
    with db.SessionLocal() as s:
        obj=s.get(db.LoginFlow,hashsecret(state))
        if not obj:raise HTTPException(400,'Solicitação expirada ou já utilizada')
        age=db.utcnow()- (obj.created_at.replace(tzinfo=timezone.utc) if obj.created_at.tzinfo is None else obj.created_at)
        verifier=decrypt(obj.verifier_encrypted)
        referral_code=obj.referral_code
        s.delete(obj);s.commit()
    if age.total_seconds()>600:raise HTTPException(400,'Solicitação de login expirada')
    if error:return RedirectResponse('/?login=cancelled',303)
    if not code:raise HTTPException(400,'Código OAuth ausente')
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            token_resp=await client.post(TOKEN_URL,data={'grant_type':'authorization_code','client_id':CLIENT_ID,
                'code':code,'code_verifier':verifier,'redirect_uri':REDIRECT})
            token_resp.raise_for_status()
            token_data=token_resp.json();token=token_data['access_token'];ttl=int(token_data['expires_in'])
            accounts_resp=await client.get('https://api.derivws.com/trading/v1/options/accounts',
                headers={'Authorization':f'Bearer {token}'})
            accounts_resp.raise_for_status();payload=accounts_resp.json()
    except (httpx.HTTPError,ValueError,KeyError):
        raise HTTPException(502,'Falha de autenticação com a Deriv') from None
    from .auth_helpers import extract_accounts, demo_id
    account=demo_id(extract_accounts(payload))
    if account is None:
        raise HTTPException(409,'A Deriv não retornou uma conta demo identificável; não foi criada uma sessão')
    session_id=make_session_id()
    with db.SessionLocal() as s:
        user=s.execute(select(db.User).where(db.User.deriv_account_id==account)).scalar_one_or_none()
        created_new=user is None
        if created_new:
            user=db.User(deriv_account_id=account);s.add(user);s.flush()
        register_first_login(s,user,created_new=created_new,referred_code=referral_code)
        autopilot_enabled.discard(user.id)
        old_consent=s.get(db.AutoPilotResumeConsent,user.id)
        if old_consent:s.delete(old_consent)
        autopilot_reasons[user.id]='Nova sessão Deriv: ative o AutoPilot manualmente'
        old_run=s.get(db.RobotRun,user.id)
        if old_run:s.delete(old_run)
        s.add(db.Session(session_hash=hashsecret(session_id),user_id=user.id, token_encrypted=encrypt(token),
                         expires_at=db.utcnow()+timedelta(seconds=max(1,ttl-30))))
        s.commit()
    response=RedirectResponse('/?login=ok',303)
    response.delete_cookie('nexora_state',path='/')
    response.set_cookie('nexora_sid',session_id,max_age=max(1,min(ttl-30,86400)),**cookie_options())
    return response

@app.get('/api/session')
async def session_info(request:Request,nexora_sid:str|None=Cookie(default=None)):
    try:u=active_user(request,nexora_sid)
    except HTTPException:return {'authenticated':False,'demo_accounts':[],'real_enabled':False}
    return {'authenticated':True,'demo_accounts':[{'account_id':u.deriv_account_id,'account_type':'demo'}],
            'real_enabled':False,'is_admin':False}

@app.get('/api/accounts/available')
async def available_accounts(request:Request,nexora_sid:str|None=Cookie(default=None)):
    """Read-only account discovery; does NOT enable real-account trading."""
    user,token=active_session(request,nexora_sid)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response=await client.get('https://api.derivws.com/trading/v1/options/accounts',
                headers={'Authorization':f'Bearer {token}'})
            response.raise_for_status()
            payload=response.json()
    except (httpx.HTTPError,ValueError):
        raise HTTPException(502,'Não foi possível verificar as contas vinculadas à Deriv') from None
    from .auth_helpers import extract_accounts,real_accounts
    found=real_accounts(extract_accounts(payload))
    return {'demo_accounts':[{'account_id':user.deriv_account_id,'account_type':'demo'}],
            'real_accounts':found,'real_trading_enabled':False,
            'execution':'DERIV_DEMO_ONLY'}


@app.get('/api/real/balance')
async def real_balance_route(request:Request,nexora_sid:str|None=Cookie(default=None)):
    """Read-only real Options account balance, never authorizes purchases.

    A balance is shown only if the Deriv account-list response itself contains
    a validated monetary amount for an explicitly real-typed account.
    """
    user,token=active_session(request,nexora_sid)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response=await client.get('https://api.derivws.com/trading/v1/options/accounts',
                headers={'Authorization':f'Bearer {token}'})
            response.raise_for_status()
            payload=response.json()
    except (httpx.HTTPError,ValueError):
        raise HTTPException(502,'Não foi possível consultar as contas reais na Deriv') from None
    from .auth_helpers import extract_accounts,real_balance
    entry=real_balance(extract_accounts(payload))
    if entry is None:
        return {'available':False,'balance':None,'currency':None,'account_id':None,
                'reason':'Nenhuma conta real vinculada foi identificada'}
    if not entry['available']:
        # Account discovery does not always include a balance. Ask the
        # authenticated real Options channel for READ-ONLY balance only.
        from .real_readonly import RealBalanceError,fetch_real_balance
        try:
            return await fetch_real_balance(token,entry['account_id'])
        except RealBalanceError:
            return {'available':False,'balance':None,'currency':entry['currency'] or None,
                    'account_id':entry['account_id'],
                    'reason':'Conta real identificada; não foi possível consultar o saldo'}
    return {'available':True,'balance':entry['balance'],'currency':entry['currency'],
            'account_id':entry['account_id'],'reason':None}



@app.get('/api/real/results/today')
async def real_results_today(request:Request,nexora_sid:str|None=Cookie(default=None)):
    """Official REAL closed-contract P/L; read-only and never conflated with DEMO."""
    user,token=active_session(request,nexora_sid)
    start,end,local_start=daily_pnl_window()
    unavailable={'available':False,'pnl':None,'currency':None,'account_id':None,
                 'date':local_start.date().isoformat(),'timezone':'America/Sao_Paulo',
                 'source':'DERIV_REAL_PROFIT_TABLE'}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response=await client.get('https://api.derivws.com/trading/v1/options/accounts',
                headers={'Authorization':f'Bearer {token}'})
            response.raise_for_status()
            payload=response.json()
        from .auth_helpers import extract_accounts,real_balance
        account=real_balance(extract_accounts(payload))
        if account is None:
            return {**unavailable,'reason':'Nenhuma conta real vinculada identificada'}
        from .real_readonly import RealBalanceError,fetch_real_daily_profit
        try:
            result=await fetch_real_daily_profit(token,account['account_id'],
                int(start.timestamp()),int(end.timestamp()))
            return {**result,'date':local_start.date().isoformat(),
                    'timezone':'America/Sao_Paulo'}
        except RealBalanceError:
            return {**unavailable,'account_id':account['account_id'],
                    'reason':'Resultado real indisponível: consulta oficial não concluída'}
    except (httpx.HTTPError,ValueError):
        return {**unavailable,'reason':'Não foi possível verificar a conta real na Deriv'}


@app.post('/api/logout')
async def logout(request:Request,nexora_sid:str|None=Cookie(default=None)):
    if nexora_sid:
        with db.SessionLocal() as s:
            session=s.get(db.Session,hashsecret(nexora_sid))
            if session:
                autopilot_enabled.discard(session.user_id)
                old_consent=s.get(db.AutoPilotResumeConsent,session.user_id)
                if old_consent:s.delete(old_consent)
                autopilot_reasons[session.user_id]='Sessão encerrada; AutoPilot desligado'
                async with lock:
                    engine=runtimes.get(session.user_id)
                    if engine:engine.running=False
                old_run=s.get(db.RobotRun,session.user_id)
                if old_run:s.delete(old_run)
                s.delete(session);s.commit()
    response=JSONResponse({'ok':True});response.delete_cookie('nexora_sid',path='/');return response

class Config(BaseModel):
    stake:str='2.00';martingale:bool=False;multiplier:str='2.00';stop_win:str='0.00';stop_loss:str='0.00'


class DemoProposal(BaseModel):
    symbol:str='1HZ100V';direction:str='CALL';minutes:int=5

class DemoBuy(DemoProposal):
    confirm:str=''
    request_key:str=''

@app.get('/api/config/current')
async def current_config(request:Request,nexora_sid:str|None=Cookie(default=None)):
    """Return saved risk preferences for the authenticated Deriv user."""
    u=active_user(request,nexora_sid)
    return {'stake':u.stake,'stop_win':u.stop_win,'stop_loss':u.stop_loss,
            'martingale':bool(u.martingale),'multiplier':u.multiplier,
            'execution':'DERIV_DEMO_ONLY','real_orders_enabled':False}


class AutoPilotSwitch(BaseModel):
    confirm:str=''


def latest_risk_session(sess,user_id):
    return sess.scalars(select(db.RiskSession).where(
        db.RiskSession.user_id==user_id).order_by(db.RiskSession.id.desc()).limit(1)).first()


def risk_session_summary(sess,user_id):
    from .demo_ledger import money
    boundary=latest_risk_session(sess,user_id)
    orders=sess.scalars(select(db.DemoOrder).where(
        db.DemoOrder.user_id==user_id)).all()
    after_id=boundary.after_order_id if boundary else 0
    pnl=sum((money(o.pnl) for o in orders
             if o.state=='SETTLED' and o.pnl is not None and o.id>after_id),
            Decimal('0'))
    return {'session_id':boundary.id if boundary else None,
            'started_at':boundary.created_at.isoformat() if boundary else None,
            'after_order_id':after_id,'pnl':str(pnl.quantize(Decimal('0.01'))),
            'execution':'DERIV_DEMO_ONLY'}


class RiskResetRequest(BaseModel):
    confirm:str


@app.get('/api/demo/risk-session')
async def get_risk_session(request:Request,nexora_sid:str|None=Cookie(default=None)):
    user=active_user(request,nexora_sid)
    with db.SessionLocal() as sess:
        return risk_session_summary(sess,user.id)


@app.post('/api/demo/risk-session/reset')
async def reset_risk_session(request:Request,payload:RiskResetRequest,
                            nexora_sid:str|None=Cookie(default=None)):
    """Manual reset changes risk-session accounting only; never deletes broker records."""
    user=active_user(request,nexora_sid)
    if payload.confirm!='RESET_DEMO_RISK_SESSION':
        raise HTTPException(403,'Confirme o reinício da sessão de treinamento')
    async with autopilot_lock:
        if user.id in autopilot_enabled:
            raise HTTPException(409,'Desligue o robô antes de reiniciar a sessão')
        with db.SessionLocal() as sess:
            pending=sess.scalars(select(db.DemoOrder).where(
                db.DemoOrder.user_id==user.id,
                db.DemoOrder.state.in_(('PENDING','CONFIRMED','UNKNOWN')))).all()
            if pending:
                raise HTTPException(409,'Existe contrato pendente ou incerto. Aguarde a liquidação e reconciliação antes de reiniciar.')
            latest=sess.scalars(select(db.DemoOrder.id).where(
                db.DemoOrder.user_id==user.id).order_by(db.DemoOrder.id.desc()).limit(1)).first()
            session=db.RiskSession(user_id=user.id,after_order_id=latest or 0)
            sess.add(session)
            # A reset revokes both the prior progression and resume consent.
            run=sess.get(db.RobotRun,user.id)
            consent=sess.get(db.AutoPilotResumeConsent,user.id)
            if run:sess.delete(run)
            if consent:sess.delete(consent)
            sess.commit()
            session_id=session.id
        autopilot_last_attempt.pop(user.id,None)
        autopilot_reasons[user.id]='Sessão de risco reiniciada; ative o robô manualmente'
    return {'ok':True,'session_id':session_id,
            'message':'Nova sessão DEMO iniciada. Histórico e saldo preservados.'}


class EvaluationResetRequest(BaseModel):
    confirm:str


@app.post('/api/autopilot/v2/new-evaluation')
async def new_v2_evaluation(request:Request,payload:EvaluationResetRequest,
                            nexora_sid:str|None=Cookie(default=None)):
    """Start independent DEMO V2.1 evaluation without deleting broker evidence."""
    user=active_user(request,nexora_sid)
    if payload.confirm!='START_NEW_DEMO_EVALUATION':
        raise HTTPException(403,'Confirme o início de nova avaliação DEMO')
    if user.selected_mode!='demo':
        raise HTTPException(403,'Somente conta DEMO')
    if not v2_enabled():
        raise HTTPException(503,'Experimento Forex indisponível')
    async with autopilot_lock:
        if user.id in autopilot_enabled:
            raise HTTPException(409,'Desligue o AutoPilot antes de iniciar nova avaliação')
        with db.SessionLocal() as sess:
            active=sess.scalars(select(db.DemoOrder).where(
                db.DemoOrder.user_id==user.id,
                db.DemoOrder.state.in_(('PENDING','CONFIRMED','UNKNOWN')))).all()
            if active:
                raise HTTPException(409,'Aguarde a liquidação ou reconciliação dos contratos abertos antes de iniciar nova avaliação')
            last=sess.scalars(select(db.DemoOrder.id).where(
                db.DemoOrder.user_id==user.id).order_by(db.DemoOrder.id.desc()).limit(1)).first()
            anchor_id=last or 0
            batch=sess.get(db.PilotValidationBatch,user.id)
            if batch is None:
                batch=db.PilotValidationBatch(user_id=user.id)
                sess.add(batch)
            batch.after_order_id=anchor_id
            batch.max_orders=200  # 2.2 marker and informational sampling goal, never an execution cap.
            batch.created_at=db.utcnow()
            sess.add(db.RiskSession(user_id=user.id,after_order_id=anchor_id))
            run=sess.get(db.RobotRun,user.id)
            consent=sess.get(db.AutoPilotResumeConsent,user.id)
            if run:sess.delete(run)
            if consent:sess.delete(consent)
            sess.commit()
        autopilot_last_attempt.pop(user.id,None)
        v2_last_scan.pop(user.id,None)
        from .trade_selector_v2 import clear_user_signals
        clear_user_signals(user.id)
        autopilot_reasons[user.id]='Nova avaliação DEMO 2.2 H1/M15 pronta; AutoPilot desligado'
    return {'ok':True,'batch':'NEXORA_2.2','after_order_id':anchor_id,
            'execution':'DERIV_DEMO_ONLY','running':False,
            'message':'Indicadores da avaliação 2.2 zerados. Histórico anterior e saldo Deriv preservados.'}


def autopilot_start_blocker(user,orders,*,after_order_id=0):
    """Read-only explanation for refused starts; never bypass order safeguards."""
    if not autopilot_allowed():
        return 'AutoPilot DEMO está desativado na configuração do servidor'
    if user.selected_mode!='demo':
        return 'Apenas a conta Treinamento está habilitada para operar'
    if user.martingale:
        return 'Configuração antiga de Martingale incompatível; salve novamente em Configurações'
    from .demo_ledger import order_guard,OrderGuardError
    try:
        order_guard(user,orders,user.stake,after_order_id=after_order_id)
    except OrderGuardError as exc:
        if 'Contrato demo em andamento' in str(exc):
            if any(o.state in ('PENDING','UNKNOWN') for o in orders):
                return 'Existe contrato pendente ou incerto; confira o contrato na página Resultados antes de ligar o robô'
            if any(o.state=='CONFIRMED' for o in orders):
                return None  # Existing confirmed contract will settle before any next buy.
        return str(exc)
    return None


@app.get('/api/autopilot/readiness')
async def autopilot_readiness(request:Request,nexora_sid:str|None=Cookie(default=None)):
    """Authenticated dry-run of start guards, without activating or purchasing."""
    user=active_user(request,nexora_sid)
    with db.SessionLocal() as sess:
        orders=sess.scalars(select(db.DemoOrder).where(db.DemoOrder.user_id==user.id)).all()
        boundary=latest_risk_session(sess,user.id)
        reason=autopilot_start_blocker(user,orders,after_order_id=boundary.after_order_id if boundary else 0)
        access=access_status(sess,user.id)
        if access['status']=='SUSPENDED' or (access['enforced'] and access['status']=='EXPIRED'):
            reason='Acesso do robô suspenso ou expirado. Conquiste horas por indicação ou consulte o suporte.'
    return {'ready':reason is None,'reason':reason,'access':access,
            'execution':'DERIV_DEMO_ONLY','real_orders_enabled':False}


@app.get('/api/access/status')
async def my_access_status(request:Request,nexora_sid:str|None=Cookie(default=None)):
    user=active_user(request,nexora_sid)
    with db.SessionLocal() as sess:
        status=access_status(sess,user.id)
        referral=referral_report(sess,user.id)
        sess.commit()
        return {**status,**referral,'user_id':'NXR-'+str(user.id).zfill(6)}


from . import admin_auth


class AdminCredentials(BaseModel):
    login:str
    password:str


@app.get('/admin')
async def admin_home():
    return FileResponse(Path(__file__).parent/'admin.html')


@app.post('/api/admin/login')
async def admin_login(request:Request,credentials:AdminCredentials):
    # Uniform errors so a caller cannot probe whether the account is enabled.
    identity=(request.client.host if request.client else 'unknown')
    if not admin_auth.login_allowed(identity):
        raise HTTPException(429,'Muitas tentativas. Tente novamente mais tarde.')
    if not (secrets.compare_digest(credentials.login,'Admin') and
            admin_auth.verify_password(credentials.password)):
        admin_auth.record_failed_login(identity)
        raise HTTPException(401,'Login ou senha inválidos')
    admin_auth.clear_failed_logins(identity)
    response=JSONResponse({'authenticated':True,'admin':'Admin'})
    response.set_cookie('nexora_admin_sid',admin_auth.session_token(),
                        max_age=admin_auth.SESSION_SECONDS,**cookie_options())
    return response


@app.get('/api/admin/session')
async def admin_session(request:Request):
    return {'authenticated':admin_auth.valid_session(
        request.cookies.get('nexora_admin_sid'))}


@app.post('/api/admin/logout')
async def admin_logout(request:Request):
    response=JSONResponse({'authenticated':False})
    response.delete_cookie('nexora_admin_sid',path='/')
    return response


def require_nexora_admin(request):
    if not admin_auth.valid_session(request.cookies.get('nexora_admin_sid')):
        raise HTTPException(401,'Entre no painel Admin para continuar')
    return 'Admin'


class AdminAccessAdjustment(BaseModel):
    user_id:int
    action:str
    hours:int=0
    note:str


def nexora_user_code(user_id:int)->str:
    return 'NXR-'+str(user_id).zfill(6)


@app.get('/api/admin/access/users')
async def admin_access_users(request:Request,q:str=''):
    require_nexora_admin(request)
    q=q.strip()
    if len(q)>100:
        raise HTTPException(422,'Busca muito longa')
    with db.SessionLocal() as sess:
        query=select(db.User)
        if q:
            match=re.fullmatch(r'(?i)NXR-?(\d{1,12})',q)
            if match:
                query=query.where(db.User.id==int(match.group(1)))
            elif q.isdecimal():
                query=query.where(db.User.id==int(q))
            else:
                query=query.where(db.User.deriv_account_id.ilike('%'+q.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%',escape='\\'))
        users=sess.scalars(query.order_by(db.User.id.desc()).limit(100)).all()
        result=[]
        for target in users:
            access=access_status(sess,target.id)
            count=sess.scalar(select(func.count(db.ReferralCredit.invitee_user_id)).where(
                db.ReferralCredit.referrer_user_id==target.id)) or 0
            result.append({'user_id':target.id,'nexora_id':nexora_user_code(target.id),
                           'deriv_account_id':target.deriv_account_id,
                           'referrals':count,'access':access})
        return {'items':result}


@app.post('/api/admin/access/adjust')
async def admin_access_adjust(request:Request,payload:AdminAccessAdjustment):
    require_nexora_admin(request)
    action=payload.action
    if action not in ('add_hours','remove_hours','suspend','resume','refund_revoke'):
        raise HTTPException(422,'Ação administrativa inválida')
    if len(payload.note.strip())<5 or len(payload.note)>255:
        raise HTTPException(422,'Informe motivo com 5 a 255 caracteres')
    if action in ('add_hours','remove_hours') and not 1<=payload.hours<=2160:
        raise HTTPException(422,'Informe horas entre 1 e 2160')
    if action not in ('add_hours','remove_hours') and payload.hours!=0:
        raise HTTPException(422,'Esta ação não recebe horas')
    with db.SessionLocal() as sess:
        subject=sess.get(db.User,payload.user_id)
        if not subject:
            raise HTTPException(404,'Usuário não encontrado')
        now=db.utcnow()
        grant=sess.get(db.RobotAccess,subject.id)
        if grant is None:
            grant=db.RobotAccess(user_id=subject.id,access_kind='admin_exception',
                                 starts_at=now,expires_at=now)
            sess.add(grant)
            sess.flush()
        if action=='add_hours':
            grant.expires_at=max(now,utc(grant.expires_at))+timedelta(hours=payload.hours)
        elif action=='remove_hours':
            grant.expires_at=max(now,utc(grant.expires_at)-timedelta(hours=payload.hours))
        elif action=='suspend':
            grant.suspended=True
        elif action=='resume':
            grant.suspended=False
        elif action=='refund_revoke':
            grant.suspended=True
            grant.expires_at=now
        sess.add(db.OwnerAdminEvent(subject_user_id=subject.id,action=action,
                   delta_hours=(payload.hours if action=='add_hours' else
                                -payload.hours if action=='remove_hours' else None),
                   note=payload.note.strip()))
        sess.commit()
        updated=access_status(sess,subject.id)
    if action in ('suspend','refund_revoke','remove_hours'):
        if not updated['allowed'] and subject.id in autopilot_enabled:
            disarm_autopilot(subject.id,
                'Acesso administrativo suspenso ou expirado; contratos existentes mantidos')
    return {'user_id':subject.id,'access':updated,'action':action}


@app.get('/api/admin/access/audit')
async def admin_access_audit(request:Request):
    require_nexora_admin(request)
    with db.SessionLocal() as sess:
        events=sess.scalars(select(db.OwnerAdminEvent).order_by(
            db.OwnerAdminEvent.id.desc()).limit(100)).all()
        return {'items':[{'at':e.created_at.isoformat(),
                          'user_id':e.subject_user_id,'action':e.action,
                          'hours':e.delta_hours,'note':e.note} for e in events]}


@app.post('/api/autopilot/start')
async def autopilot_start(request:Request,payload:AutoPilotSwitch,
                          nexora_sid:str|None=Cookie(default=None)):
    user,_=active_session(request,nexora_sid)
    if payload.confirm!='START_DEMO_AUTOPILOT':
        raise HTTPException(403,'Confirme o modo automático de treinamento')
    if not autopilot_allowed():
        raise HTTPException(503,'AutoPilot DEMO não habilitado no servidor')
    if user.selected_mode!='demo':
        raise HTTPException(403,'Somente conta demonstrativa')
    if user.martingale:
        raise HTTPException(409,'Configuração antiga de Martingale: salve novamente as opções atuais')
    with db.SessionLocal() as sess:
        orders=sess.scalars(select(db.DemoOrder).where(db.DemoOrder.user_id==user.id)).all()
        boundary=latest_risk_session(sess,user.id)
        blocker=autopilot_start_blocker(user,orders,after_order_id=boundary.after_order_id if boundary else 0)
        if blocker:
            raise HTTPException(409,blocker)
        access=access_status(sess,user.id)
        if access['status']=='SUSPENDED' or (access['enforced'] and access['status']=='EXPIRED'):
            raise HTTPException(403,'Acesso suspenso ou expirado. Novas entradas desabilitadas.')
    # Each manual activation starts a new durable sequence. Historical settled
    # orders still apply to cumulative stop checks but not the new progression.
    async with autopilot_lock:
        with db.SessionLocal() as sess:
            access=start_trial_on_first_robot_start(sess,user.id)
            if access['enforced'] and not access['allowed']:
                raise HTTPException(403,'Acesso do robô expirado. Renove antes de ligar.')
            existing=sess.get(db.RobotRun,user.id)
            latest=sess.scalars(select(db.DemoOrder.id).where(
                db.DemoOrder.user_id==user.id).order_by(db.DemoOrder.id.desc()).limit(1)).first()
            if not existing:
                existing=db.RobotRun(user_id=user.id);sess.add(existing)
            existing.after_order_id=latest or 0
            existing.started_at=db.utcnow()
            if v2_enabled():
                batch=sess.get(db.PilotValidationBatch,user.id)
                if batch is None:
                    # New users start with the current 2.2 H1/M15 scanner.
                    # Existing orders remain outside this first evaluation.
                    sess.add(db.PilotValidationBatch(
                        user_id=user.id,after_order_id=latest or 0,max_orders=200))
                elif batch.max_orders<200:
                    # The UI is 2.2: never silently run the older M1 scanner.
                    # Preserve this evaluation's original after_order_id,
                    # orders, official results, and risk-session boundary.
                    batch.max_orders=200
            if not sess.get(db.AutoPilotResumeConsent,user.id):
                sess.add(db.AutoPilotResumeConsent(user_id=user.id))
            sess.commit()
        autopilot_last_attempt.pop(user.id,None)
        v2_last_scan.pop(user.id,None)
        autopilot_enabled.add(user.id)
        log.warning('AutoPilot armed: scan_mode=%s; market=automatic; execution=DEMO',
                 'V2.2_H1_M15' if v2_enabled() else 'LEGACY')
    autopilot_reasons[user.id]='AutoPilot ligado; aguardando oportunidade oficial na Deriv Demo'
    return {'running':True,'access':access,'execution':'DERIV_DEMO_ONLY','real_orders_enabled':False}


@app.post('/api/autopilot/stop')
async def autopilot_stop(request:Request,nexora_sid:str|None=Cookie(default=None)):
    user=active_user(request,nexora_sid)
    # Wait for any already dispatched broker request to finish before declaring OFF.
    async with autopilot_lock:
        autopilot_enabled.discard(user.id)
        for key in list(v2_gate_last_log):
            if key[0]==user.id: v2_gate_last_log.pop(key,None)
        with db.SessionLocal() as sess:
            run=sess.get(db.RobotRun,user.id)
            consent=sess.get(db.AutoPilotResumeConsent,user.id)
            if run:sess.delete(run)
            if consent:sess.delete(consent)
            sess.commit()
        autopilot_reasons[user.id]='Desligado: novas entradas bloqueadas; contratos abertos permanecem na Deriv'
    return {'running':False,'execution':'DERIV_DEMO_ONLY','real_orders_enabled':False}


@app.get('/api/status')
async def status(request:Request,nexora_sid:str|None=Cookie(default=None)):
    user=active_user(request,nexora_sid)
    with db.SessionLocal() as sess:
        interrupted=sess.get(db.RobotRun,user.id) is not None and user.id not in autopilot_enabled
        batch=sess.get(db.PilotValidationBatch,user.id)
        mode='V2_MULTI_ASSET' if batch else 'V1_SINGLE_ASSET'
        upgrade_needed=bool(v2_enabled() and batch is None and user.id in autopilot_enabled)
    async with lock:
        engine=rt_for(user)
        return {'connected':engine.connected,'symbol':engine.symbol,
                'latest_price':engine.prices[-1] if engine.prices else None,
                'ticks':len(engine.prices),'execution':'DERIV_DEMO_ONLY',
                'running':user.id in autopilot_enabled,'auto_buy_enabled':autopilot_allowed(),
                'real_orders_enabled':False,
                'interrupted_after_restart':interrupted,
                'autopilot_version':mode,
                'v2_available':v2_enabled(),
                'v2_upgrade_needed':upgrade_needed,
                'analysis_symbol':SYMBOL,
                'reason':autopilot_reasons.get(user.id,
                   'Execução interrompida após reinício; reative manualmente no painel' if interrupted else 'Robô desligado')}

@app.get('/api/autopilot/v2/scanner-status')
async def autopilot_v2_scanner_status(request:Request,
                                     nexora_sid:str|None=Cookie(default=None)):
    """Read-only diagnostics distinguishing data failures from absent signals."""
    user=active_user(request,nexora_sid)
    with db.SessionLocal() as sess:
        batch=sess.get(db.PilotValidationBatch,user.id)
    version=('NEXORA_2.2' if batch and batch.max_orders>=200 else
             'NEXORA_2.1' if batch and batch.max_orders>=100 else 'NEXORA_2.0')
    return {'version':version,'v2_enabled':v2_enabled(),
            'v2_session_active':bool(batch),'running':user.id in autopilot_enabled,
            'scan':last_scan_report(),'real_orders_enabled':False}


@app.get('/api/autopilot/research')
async def autopilot_research(request:Request,nexora_sid:str|None=Cookie(default=None)):
    """Read-only market research; does not submit any trading order."""
    user=active_user(request,nexora_sid)
    async with lock:
        engine=rt_for(user)
        prices=list(engine.prices)
        connected=engine.connected
    return {**analyze(prices),'connected':connected,'symbol':SYMBOL,
            'execution':'RESEARCH_ONLY','auto_buy_enabled':False}


@app.get('/api/autopilot/demo-preview')
async def autopilot_demo_preview(request:Request,nexora_sid:str|None=Cookie(default=None)):
    """Read-only bridge from hypotheses to Deriv DEMO proposals."""
    user,token=active_session(request,nexora_sid)
    if user.selected_mode!='demo':
        raise HTTPException(403,'Selecione a conta de treinamento')
    async with lock:
        engine=rt_for(user)
        prices=list(engine.prices)
        connected=engine.connected
    if not connected:
        raise HTTPException(409,'Cotações desconectadas')
    research=analyze(prices)
    if not research['candidates']:
        return {'state':research['state'],'symbol':SYMBOL,'samples':research['samples'],
                'candidate':None,'proposal':None,'bought':False,'reason':research['reason']}
    candidate=research['candidates'][0]
    try:
        quote=await demo_proposal(token,user.deriv_account_id,symbol=SYMBOL,
                                  contract_type=candidate['direction'],stake=user.stake,
                                  duration=5)
        evaluation=quote_assessment(quote['ask_price'],quote['payout'])
    except DemoTradingError as e:
        raise HTTPException(502,str(e)) from None
    except (ValueError,KeyError):
        raise HTTPException(502,'Proposta demonstrativa retornou valores inválidos') from None
    return {'state':'DEMO_QUOTE_RESEARCH','symbol':SYMBOL,'samples':research['samples'],
            'candidate':candidate,'proposal':quote,'evaluation':evaluation,'bought':False,
            'reason':'Proposta oficial de treinamento para análise, sem compra'}


@app.post('/api/config')
async def config(request:Request, config:Config,nexora_sid:str|None=Cookie(default=None)):
    """Legacy API retains flat stake for older integrations; UI uses onboarding."""
    u=active_user(request,nexora_sid)
    if u.id in autopilot_enabled:
        raise HTTPException(409,'Desligue o robô antes de alterar as configurações')
    if config.martingale:
        raise HTTPException(422,'Configure Martingale com quantidade de tentativas no painel atualizado')
    try:settings=Settings.parse(config.model_dump())
    except ValueError as e:raise HTTPException(422,str(e)) from None
    async with lock:
        engine=rt_for(u)
        try:engine.configure(settings)
        except ValueError as e:raise HTTPException(409,str(e)) from None
        with db.SessionLocal() as sess:
            stored=sess.get(db.User,u.id)
            stored.stake=str(settings.stake);stored.stop_win=str(settings.stop_win);stored.stop_loss=str(settings.stop_loss)
            stored.multiplier='2.00';stored.martingale=False
            sess.commit()
    return {'ok':True}


class OnboardingConfig(BaseModel):
    stake:str='2.00'
    stop_win:str='0.00'
    stop_loss:str='0.00'
    martingale:bool=False
    gale_count:int=0
    multiplier:str='2.00'
    cycles:str='0'
    cycle_multiplier:str='2.00'


def parse_progression_multiplier(value, label):
    from decimal import Decimal, InvalidOperation
    try:
        normalized=str(value).strip().replace(',','.')
        d=Decimal(normalized)
    except (InvalidOperation,ValueError,TypeError):
        raise HTTPException(422,label+': informe um número válido, como 1.0, 2.3 ou 2.5') from None
    if not d.is_finite() or d<1:
        raise HTTPException(422,label+': informe um número maior ou igual a 1.0')
    if d.as_tuple().exponent<-2:
        raise HTTPException(422,label+': use no máximo duas casas decimais')
    if d>100000:
        raise HTTPException(422,label+': valor acima do limite de 100000')
    return d


@app.get('/api/onboarding/config')
async def get_onboarding_config(request:Request,nexora_sid:str|None=Cookie(default=None)):
    u=active_user(request,nexora_sid)
    with db.SessionLocal() as sess:
        plan=sess.get(db.RobotPlan,u.id)
        cycles=sess.get(db.RobotCycleParams,u.id)
        return {'mode':'demo','real_enabled':False,
                'stake':u.stake,'stop_win':u.stop_win,'stop_loss':u.stop_loss,
                'martingale':bool(plan.requested_martingale) if plan else False,
                'gale_count':plan.gale_count if plan else 0,
                'multiplier':plan.requested_multiplier if plan else '2.00',
                'cycles':str(plan.cycles_limit) if plan and plan.cycles_limit is not None else '0',
                'cycle_multiplier':cycles.multiplier if cycles else '2.00',
                'martingale_execution_enabled':True,'cycles_execution_enabled':True}


@app.post('/api/onboarding/config')
async def save_onboarding_config(request:Request,payload:OnboardingConfig,
                                 nexora_sid:str|None=Cookie(default=None)):
    u=active_user(request,nexora_sid)
    if u.id in autopilot_enabled:
        raise HTTPException(409,'Desligue o robô antes de salvar uma nova progressão')
    if payload.gale_count not in range(0,6):
        raise HTTPException(422,'Quantidade de Martingales deve estar entre 0 e 5')
    if payload.cycles not in ('0','1','2','3','4','5'):
        raise HTTPException(422,'Quantidade de ciclos deve estar entre 0 e 5')
    if payload.martingale and payload.gale_count==0:
        raise HTTPException(422,'Para ativar Martingale, escolha entre 1 e 5 tentativas')
    if not payload.martingale and payload.gale_count!=0:
        raise HTTPException(422,'Desative a quantidade de tentativas quando Martingale estiver desligado')
    gale_factor=parse_progression_multiplier(payload.multiplier,'Multiplicador Martingale')
    cycle_factor=parse_progression_multiplier(payload.cycle_multiplier,'Multiplicador de ciclos')
    try:
        settings=Settings.parse({'stake':payload.stake,'stop_win':payload.stop_win,
                                 'stop_loss':payload.stop_loss,
                                 'martingale':False,'multiplier':'2'})
    except ValueError as e:
        raise HTTPException(422,str(e)) from None
    from .progression import CAP_PER_TRADE
    if settings.stake>CAP_PER_TRADE:
        raise HTTPException(422,'Entrada inicial excede o limite de US$ 100 da sequência')
    async with autopilot_lock:
        if u.id in autopilot_enabled:
            raise HTTPException(409,'Desligue o robô antes de salvar uma nova progressão')
        async with lock:
            engine=rt_for(u)
            try:engine.configure(settings)
            except ValueError as e:raise HTTPException(409,str(e)) from None
            with db.SessionLocal() as sess:
                pending=sess.scalars(select(db.DemoOrder).where(
                    db.DemoOrder.user_id==u.id, db.DemoOrder.state.in_(('PENDING','CONFIRMED','UNKNOWN')))).all()
                if pending:
                    raise HTTPException(409,'Aguarde o encerramento e a reconciliação do contrato antes de editar')
                stored=sess.get(db.User,u.id)
                stored.stake=str(settings.stake)
                stored.stop_win=str(settings.stop_win)
                stored.stop_loss=str(settings.stop_loss)
                stored.martingale=False
                stored.multiplier='2.00'
                plan=sess.get(db.RobotPlan,u.id)
                if not plan:
                    plan=db.RobotPlan(user_id=u.id);sess.add(plan)
                plan.requested_martingale=payload.martingale
                plan.gale_count=payload.gale_count
                plan.requested_multiplier=str(gale_factor)
                plan.cycles_limit=int(payload.cycles)
                cp=sess.get(db.RobotCycleParams,u.id)
                if not cp:
                    cp=db.RobotCycleParams(user_id=u.id);sess.add(cp)
                cp.multiplier=str(cycle_factor)
                # Changing configuration invalidates any previously armed sequence.
                run=sess.get(db.RobotRun,u.id)
                if run:
                    sess.delete(run)
                sess.commit()
    return {'ok':True,'execution_mode':'DERIV_DEMO_ONLY',
            'martingale_execution_enabled':True,'cycles_execution_enabled':True,
            'message':'Configuração salva. Ative o AutoPilot separadamente no painel.'}


# The 30-day single-symbol study is isolated from all trading controls.
_single_asset_lock=asyncio.Lock()
_single_asset_cache=None
_single_asset_cache_at=0.0

@app.get('/api/research/volatility100-30d')
async def single_asset_research_route(request:Request,nexora_sid:str|None=Cookie(default=None)):
    """Authenticated read-only historical study; zero broker buys."""
    active_user(request,nexora_sid)
    global _single_asset_cache, _single_asset_cache_at
    async with _single_asset_lock:
        now=asyncio.get_running_loop().time()
        if _single_asset_cache is not None and now-_single_asset_cache_at<3600:
            return {**_single_asset_cache,'cached':True}
        try:
            report=await run_single_asset_study()
        except MarketError as exc:
            raise HTTPException(502,str(exc)) from None
        _single_asset_cache=report
        _single_asset_cache_at=now
        return {**report,'cached':False}


# Explicit, read-only research endpoint; never touches AutoPilot execution settings.
_strategy_lab_lock=asyncio.Lock()
_strategy_lab_cache=None
_strategy_lab_cache_time=0.0

@app.get('/api/research/strategy-lab')
async def strategy_lab_route(request:Request,nexora_sid:str|None=Cookie(default=None)):
    active_user(request,nexora_sid)
    global _strategy_lab_cache, _strategy_lab_cache_time
    async with _strategy_lab_lock:
        now=asyncio.get_running_loop().time()
        if _strategy_lab_cache is not None and now-_strategy_lab_cache_time<300:
            return {**_strategy_lab_cache,'cached':True}
        try:
            report=await run_lab()
        except MarketError as exc:
            raise HTTPException(502,str(exc)) from None
        _strategy_lab_cache=report
        _strategy_lab_cache_time=now
        return {**report,'cached':False}



_asset_scanner_lock=asyncio.Lock()
_asset_scanner_cache=None
_asset_scanner_cache_time=0.0

@app.get('/api/research/asset-scanner')
async def asset_scanner_route(request:Request,nexora_sid:str|None=Cookie(default=None)):
    """Authenticated read-only market research, independent of AutoPilot."""
    active_user(request,nexora_sid)
    global _asset_scanner_cache,_asset_scanner_cache_time
    async with _asset_scanner_lock:
        now=asyncio.get_running_loop().time()
        if _asset_scanner_cache is not None and now-_asset_scanner_cache_time<300:
            return {**_asset_scanner_cache,'cached':True}
        try:
            report=await scan_assets()
        except MarketError as exc:
            raise HTTPException(502,str(exc)) from None
        _asset_scanner_cache=report
        _asset_scanner_cache_time=now
        return {**report,'cached':False}


@app.get('/api/market/symbols')
async def symbols(request:Request,nexora_sid:str|None=Cookie(default=None)):
    active_user(request,nexora_sid)
    try:return {'symbols':await discover_symbols()}
    except MarketError as e:raise HTTPException(502,str(e)) from None

@app.get('/api/market/contracts')
async def contracts(request:Request,symbol:str,nexora_sid:str|None=Cookie(default=None)):
    active_user(request,nexora_sid)
    if not re.fullmatch(r'[A-Za-z0-9_]{2,30}',symbol):raise HTTPException(422,'Ativo inválido')
    try:return {'symbol':symbol,'types':await discover_contracts(symbol)}
    except MarketError as e:raise HTTPException(502,str(e)) from None

@app.get('/api/market/quote')
async def quote(request:Request,symbol:str='1HZ100V',direction:str='CALL',minutes:int=5,
                nexora_sid:str|None=Cookie(default=None)):
    u=active_user(request,nexora_sid)
    if not re.fullmatch(r'[A-Za-z0-9_]{2,30}',symbol):raise HTTPException(422,'Ativo inválido')
    if direction not in ('CALL','PUT') or not 1<=minutes<=60:raise HTTPException(422,'Parâmetros inválidos')
    try:
        kinds=await discover_contracts(symbol)
        if direction not in kinds:raise HTTPException(409,'Contrato indisponível')
        return await indicative_proposal(symbol,stake=u.stake,contract_type=direction,duration=minutes)
    except MarketError as e:raise HTTPException(502,str(e)) from None

@app.get('/api/demo/capabilities')
async def demo_capabilities(request:Request,nexora_sid:str|None=Cookie(default=None)):
    user=active_user(request,nexora_sid)
    return {'demo_account_selected':user.selected_mode=='demo',
            'manual_demo_purchase_enabled':os.getenv('NEXORA_DEMO_BUY_ENABLED','false').lower()=='true',
            'real_orders_enabled':False,
            'automatic_demo_orders_enabled':autopilot_allowed(),
            'explanation':'AutoPilot DEMO experimental com consentimento e stops; operação REAL indisponível.'}

@app.get('/api/demo/balance')
async def demo_balance_route(request:Request,nexora_sid:str|None=Cookie(default=None)):
    user,token=active_session(request,nexora_sid)
    if user.selected_mode!='demo':
        raise HTTPException(403,'Somente conta demonstrativa')
    try:
        return await demo_account_balance(token,user.deriv_account_id)
    except DemoTradingError as e:
        raise HTTPException(502,str(e)) from None

@app.post('/api/demo/proposal')
async def demo_proposal_route(request:Request, payload:DemoProposal,
                              nexora_sid:str|None=Cookie(default=None)):
    """Read-only proposal from the authenticated demo account; never buys."""
    user, token = active_session(request, nexora_sid)
    if user.selected_mode!='demo':
        raise HTTPException(403,'Somente conta demonstrativa')
    if not re.fullmatch(r'[A-Za-z0-9_]{2,30}', payload.symbol):
        raise HTTPException(422,'Ativo inválido')
    try:
        return await demo_proposal(token, user.deriv_account_id, symbol=payload.symbol,
                                   contract_type=payload.direction, stake=user.stake,
                                   duration=payload.minutes)
    except DemoTradingError as e:
        raise HTTPException(502,str(e)) from None

async def perform_demo_buy(user, token, payload:DemoBuy, *, attribution:dict|None=None, stake_override=None, latest_buy_send_epoch=None):
    """Single guarded broker purchase path for both manual and AutoPilot DEMO orders."""
    if payload.confirm != 'DEMO_ONLY':
        raise HTTPException(403,'Confirmação de conta demo obrigatória')
    if os.getenv('NEXORA_DEMO_BUY_ENABLED','false').lower() != 'true':
        raise HTTPException(503,'Compra demonstrativa desabilitada')
    if user.selected_mode!='demo':
        raise HTTPException(403,'Somente conta demonstrativa')
    if not re.fullmatch(r'[A-Za-z0-9_]{2,30}',payload.symbol):
        raise HTTPException(422,'Ativo inválido')
    with db.SessionLocal() as s:
        stored=s.get(db.User,user.id)
        # Subscription is checked at the durable purchase boundary, not only
        # once per scanner cycle. Broker-settled open contracts remain intact.
        if attribution:
            entitlement=access_status(s,user.id)
            if not entitlement['allowed'] and entitlement['status']!='TRIAL_AVAILABLE':
                raise HTTPException(403,'Acesso ao robô expirado ou suspenso. Nenhuma nova compra enviada.')
        try:
            order,created=reserve(s,stored,request_key=payload.request_key,
                                  symbol=payload.symbol,direction=payload.direction,
                                  minutes=payload.minutes,stake_override=stake_override,
                                  parallel=bool(attribution and attribution.get('v2')))
        except OrderGuardError as e:
            raise HTTPException(409,str(e)) from None
        order_id=order.id
        if not created:
            return {**public_order(order),'replayed':True,'no_new_buy':True}
        if attribution:
            # Persist attribution and execution progression BEFORE broker I/O;
            # never claim a Martingale or Cycle from the stake alone.
            s.add(db.AutoPilotAttribution(order_id=order_id,user_id=user.id,
                   strategy=attribution['strategy'],direction=payload.direction,
                   reason=str(attribution['reason'])[:255]))
            if 'cycle_index' in attribution and 'gale_index' in attribution:
                s.add(db.DemoOrderProgression(order_id=order_id,user_id=user.id,
                    cycle_index=int(attribution['cycle_index']),
                    gale_index=int(attribution['gale_index']),
                    gale_multiplier=str(attribution['gale_multiplier']),
                    cycle_multiplier=str(attribution['cycle_multiplier']),
                    source='autopilot',account_mode='demo'))
            s.commit()
    # Never hold DB transaction across remote Deriv call.
    try:
        buy_kwargs={'symbol':payload.symbol,'contract_type':payload.direction,
                    'stake':order.stake,'duration':payload.minutes,'confirm':'DEMO_ONLY'}
        if attribution and attribution.get('v2'):
            buy_kwargs['min_payout_ratio']='1.50'
        if latest_buy_send_epoch is not None:
            buy_kwargs['latest_buy_send_epoch']=latest_buy_send_epoch
            if time.time()>latest_buy_send_epoch:
                raise DemoPreBuyDeadlineExceeded('Janela de compra DEMO expirada')
        receipt=await demo_buy(token, user.deriv_account_id, **buy_kwargs)
        if latest_buy_send_epoch is not None:
            target=latest_buy_send_epoch-3.0
            purchase=receipt.get('broker_purchase_time')
            start=receipt.get('broker_start_time')
            log.warning('V2.4 DEMO broker timing: order=%s purchase_offset=%s start_offset=%s',
                        order_id,
                        ('%.3f' % (float(purchase)-target)) if purchase is not None else 'UNAVAILABLE',
                        ('%.3f' % (float(start)-target)) if start is not None else 'UNAVAILABLE')
    except DemoPreBuyDeadlineExceeded:
        # Never sent to broker: safely release reserved DB slot.
        with db.SessionLocal() as s:
            pending=s.get(db.DemoOrder,order_id)
            if pending and pending.state=='PENDING':
                pending.state='CANCELLED'
                pending.active_slot=None
                pending.error_summary='V2.4: janela expirada antes do envio'
                s.commit()
        log.warning('V2.4 expired pre-buy: order=%s',order_id)
        raise HTTPException(409,'Sinal expirou antes do envio. Nenhuma compra realizada.') from None
    except Exception as exc:
        log.warning('Demo buy outcome unknown: order=%s error=%s',order_id,type(exc).__name__)
        try:
            with db.SessionLocal() as s:
                order=s.get(db.DemoOrder,order_id)
                if order: unknown(s,order)
        except Exception as persist_exc:
            log.warning('Could not persist unknown demo state: order=%s error=%s',
                        order_id,type(persist_exc).__name__)
        raise HTTPException(502,'Resultado da tentativa não confirmado. Novas compras bloqueadas até reconciliação.') from None
    with db.SessionLocal() as s:
        order=s.get(db.DemoOrder,order_id)
        try:
            confirmed(s,order,receipt)
        except Exception as exc:
            # Broker may have bought the contract; never retry the purchase.
            log.warning('Demo confirmation persistence failed: order=%s error=%s',
                        order_id,type(exc).__name__)
            try:
                with db.SessionLocal() as recovery:
                    pending=recovery.get(db.DemoOrder,order_id)
                    if pending: unknown(recovery,pending)
            except Exception as persist_exc:
                log.warning('Could not mark demo order UNKNOWN: order=%s error=%s',
                            order_id,type(persist_exc).__name__)
            raise HTTPException(503,'Contrato comprado; falha ao registrar confirmação. Não repetir compra.') from None
        return {**public_order(order),'replayed':False,'demo_only':True}


@app.post('/api/demo/buy')
async def demo_buy_route(request:Request,payload:DemoBuy,
                         nexora_sid:str|None=Cookie(default=None)):
    """One manually confirmed official DEMO purchase."""
    user,token=active_session(request,nexora_sid)
    if user.id in autopilot_enabled:
        raise HTTPException(409,'Desligue o AutoPilot antes de iniciar um teste manual')
    return await perform_demo_buy(user,token,payload)

@app.get('/api/autopilot/strategy-stats')
async def autopilot_strategy_stats(request:Request,nexora_sid:str|None=Cookie(default=None)):
    """Official settled-broker results attributed to an experimental strategy."""
    user=active_user(request,nexora_sid)
    from decimal import Decimal
    with db.SessionLocal() as session:
        batch=session.get(db.PilotValidationBatch,user.id)
        statement=(select(db.AutoPilotAttribution, db.DemoOrder)
            .join(db.DemoOrder,db.AutoPilotAttribution.order_id==db.DemoOrder.id)
            .where(db.AutoPilotAttribution.user_id==user.id))
        if batch:
            statement=statement.where(db.DemoOrder.id>batch.after_order_id)
        records=session.execute(statement).all()
    summary={}
    for attribution,order in records:
        if order.state=='CANCELLED':
            continue  # Never transmitted to the broker; not a trade.
        item=summary.setdefault(attribution.strategy,{'strategy':attribution.strategy,
            'settled':0,'wins':0,'losses':0,'draws':0,'pending':0,'net_pnl':'0.00',
            'win_rate':None,'evidence':'EXPERIMENTAL'})
        if order.state!='SETTLED' or order.pnl is None:
            item['pending']+=1
            continue
        amount=Decimal(str(order.pnl))
        if not amount.is_finite():
            continue
        item['settled']+=1
        item['wins']+=int(amount>0)
        item['losses']+=int(amount<0)
        item['draws']+=int(amount==0)
        item['net_pnl']=str(Decimal(item['net_pnl'])+amount)
    for item in summary.values():
        if item['settled']:
            item['win_rate']=round(item['wins']/item['settled'],4)
    return {'items':list(summary.values()),'execution':'DERIV_DEMO_ONLY',
            'batch':('NEXORA_2.2' if batch.max_orders>=200 else 'NEXORA_2.1' if batch.max_orders>=100 else 'NEXORA_2.0') if batch else 'PREVIOUS',
            'note':'Somente contratos oficiais desde o início da avaliação 2.0, quando ativa. '
                   'Não comprova desempenho futuro.'}


BRASILIA_TZ=ZoneInfo('America/Sao_Paulo')


def daily_pnl_window(now=None):
    """Start of today in Brazil, expressed as UTC instants, independent of UTC date."""
    instant=now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError('Um instante com fuso horário é obrigatório')
    local=instant.astimezone(BRASILIA_TZ)
    midnight=local.replace(hour=0,minute=0,second=0,microsecond=0)
    return midnight.astimezone(timezone.utc),instant.astimezone(timezone.utc),midnight


def today_demo_pnl(session,user_id,now=None):
    """Official settled P/L only, with SQL range over all orders (not UI's 100)."""
    start,end,local_start=daily_pnl_window(now)
    orders=session.scalars(select(db.DemoOrder).where(
        db.DemoOrder.user_id==user_id,
        db.DemoOrder.state=='SETTLED',
        db.DemoOrder.updated_at>=start,
        db.DemoOrder.updated_at<=end)).all()
    total=Decimal('0')
    for order in orders:
        if order.pnl is None:
            continue
        try:
            number=Decimal(str(order.pnl))
        except (InvalidOperation,ValueError,TypeError):
            continue
        if number.is_finite():
            total+=number
    tomorrow=(local_start+timedelta(days=1))
    return {'pnl':str(total.quantize(Decimal('0.01'))),
            'date':local_start.date().isoformat(),
            'timezone':'America/Sao_Paulo',
            'next_reset_at':tomorrow.isoformat()}


@app.get('/api/demo/results/today')
async def demo_results_today(request:Request,nexora_sid:str|None=Cookie(default=None)):
    user=active_user(request,nexora_sid)
    with db.SessionLocal() as sess:
        result=today_demo_pnl(sess,user.id)
        # This endpoint is deliberately the actual account-wide official daily
        # DEMO P/L, not the isolated V2 evaluation statistic.
        return {**result,'scope':'ALL_DEMO_TODAY'}


@app.get('/api/autopilot/v2/evaluation')
async def v2_evaluation(request:Request,nexora_sid:str|None=Cookie(default=None)):
    """Official broker-settled V2-only results; previous ledger is not deleted."""
    user=active_user(request,nexora_sid)
    with db.SessionLocal() as sess:
        batch=sess.get(db.PilotValidationBatch,user.id)
        if not batch:
            return {'active':False,'submitted':0,'settled':0,'wins':0,'losses':0,
                    'pending':0,'net_pnl':'0.00','target':50,'target_is_limit':False,
                    'execution':'DERIV_DEMO_ONLY'}
        orders=sess.scalars(select(db.DemoOrder).where(
            db.DemoOrder.user_id==user.id,db.DemoOrder.id>batch.after_order_id)).all()
        attributed_ids=set(sess.scalars(select(db.AutoPilotAttribution.order_id).where(
            db.AutoPilotAttribution.user_id==user.id,
            db.AutoPilotAttribution.order_id>batch.after_order_id)).all())
        v2orders=[o for o in orders if o.id in attributed_ids and o.state!='CANCELLED']
        settled=[o for o in v2orders if o.state=='SETTLED' and o.pnl is not None]
        profit=sum((Decimal(str(o.pnl)) for o in settled),Decimal('0'))
        return {'active':True,'submitted':len(v2orders),'settled':len(settled),
                'wins':sum(Decimal(str(o.pnl))>0 for o in settled),
                'losses':sum(Decimal(str(o.pnl))<0 for o in settled),
                'pending':len(v2orders)-len(settled),'net_pnl':str(profit),
                'target':batch.max_orders,'target_is_limit':False,'execution':'DERIV_DEMO_ONLY',
                'batch':('NEXORA_2.2' if batch.max_orders>=200 else 'NEXORA_2.1' if batch.max_orders>=100 else 'NEXORA_2.0'),
                'started_at':batch.created_at.isoformat(),
                'note':'Estatística DEMO apenas desta avaliação. Contratos anteriores preservados; meta sem limite automático.'}


def order_result_details(order,progression=None,was_automatic=False):
    """Historical snapshots are factual; unknown progression stays unclassified."""
    basic=public_order(order)
    basic['duration_minutes']=order.duration_minutes
    opened=order.broker_purchase_at
    if opened is not None:
        if opened.tzinfo is None:
            opened=opened.replace(tzinfo=timezone.utc)
        basic['started_at_brasilia']=opened.astimezone(BRASILIA_TZ).isoformat()
    else:
        basic['started_at_brasilia']=None
    basic['time_source']='DERIV' if order.broker_purchase_at is not None else 'NOT_REPORTED'
    basic['account_mode']='demo'  # This table is only the official DEMO ledger.
    basic['outcome']='PENDING'
    if order.state=='SETTLED' and order.pnl is not None:
        try:
            pnl=Decimal(str(order.pnl))
        except (InvalidOperation,ValueError,TypeError):
            pnl=None
        if pnl is not None and pnl.is_finite():
            basic['outcome']='WIN' if pnl>0 else ('LOSS' if pnl<0 else 'DRAW')
        else:
            basic['outcome']='UNKNOWN'
    elif order.state=='UNKNOWN':
        basic['outcome']='UNKNOWN'
    if progression is not None:
        basic['progression']={
            'source':progression.source,
            'cycle':progression.cycle_index,
            'martingale':progression.gale_index,
            'gale_multiplier':progression.gale_multiplier,
            'cycle_multiplier':progression.cycle_multiplier}
    elif was_automatic:
        basic['progression']={'source':'autopilot','cycle':None,'martingale':None}
    else:
        basic['progression']={'source':'manual' if not was_automatic else 'unknown',
                              'cycle':None,'martingale':None}
    return basic


@app.get('/api/demo/orders')
async def demo_orders_route(request:Request,nexora_sid:str|None=Cookie(default=None)):
    user=active_user(request,nexora_sid)
    # A rolling 72-hour presentation window. Old ledger records remain in
    # Postgres for reconciliation, progression, auditability and risk checks.
    cutoff=datetime.now(timezone.utc)-timedelta(days=3)
    with db.SessionLocal() as s:
        batch=s.get(db.PilotValidationBatch,user.id)
        stmt=select(db.DemoOrder).where(db.DemoOrder.user_id==user.id,
                                       db.DemoOrder.created_at>=cutoff,
                                       db.DemoOrder.state!='CANCELLED')
        if batch:
            stmt=stmt.where(db.DemoOrder.id>batch.after_order_id)
        orders=s.scalars(stmt.order_by(db.DemoOrder.id.desc()).limit(100)).all()
        order_ids=[o.id for o in orders]
        progressions=s.scalars(select(db.DemoOrderProgression).where(
            db.DemoOrderProgression.order_id.in_(order_ids))).all() if order_ids else []
        by_order={p.order_id:p for p in progressions}
        attributions=s.scalars(select(db.AutoPilotAttribution).where(
            db.AutoPilotAttribution.order_id.in_(order_ids))).all() if order_ids else []
        automatic_ids={a.order_id for a in attributions}
        return {'items':[order_result_details(o,by_order.get(o.id),
                              o.id in automatic_ids) for o in orders],
                'execution':'DERIV_DEMO_ONLY','timezone':'America/Sao_Paulo',
                'retention_days':3,'history_cutoff':cutoff.isoformat(),
                'batch':('NEXORA_2.1' if batch.max_orders>=100 else 'NEXORA_2.0') if batch else 'PREVIOUS'}

@app.post('/api/demo/orders/{order_id}/reconcile')
async def reconcile_demo_route(request:Request,order_id:int,
                               nexora_sid:str|None=Cookie(default=None)):
    user,token=active_session(request,nexora_sid)
    async with reconcile_lock:
        with db.SessionLocal() as s:
            order=s.scalar(select(db.DemoOrder).where(db.DemoOrder.id==order_id,
                                                      db.DemoOrder.user_id==user.id))
            if not order:
                raise HTTPException(404,'Ordem demonstrativa não encontrada')
            if not order.contract_id:
                raise HTTPException(409,'Sem ID confirmado. Reconciliação manual na Deriv necessária; novas compras permanecem bloqueadas.')
            contract_id=order.contract_id
        try:
            receipt=await demo_contract_status(token,user.deriv_account_id,contract_id)
        except DemoTradingError as e:
            raise HTTPException(502,str(e)) from None
        with db.SessionLocal() as s:
            order=s.scalar(select(db.DemoOrder).where(db.DemoOrder.id==order_id,
                                                      db.DemoOrder.user_id==user.id))
            if not order:
                raise HTTPException(404,'Ordem não encontrada')
            if order.state=='SETTLED':
                return public_order(order)
            try:
                settle(s,order,receipt['contract'])
            except OrderGuardError as e:
                raise HTTPException(502,str(e)) from None
            return public_order(order)

@app.get('/api/demo/contract/{contract_id}')
async def demo_contract_route(request:Request, contract_id:str,
                              nexora_sid:str|None=Cookie(default=None)):
    user, token = active_session(request, nexora_sid)
    with db.SessionLocal() as s:
        owned=s.scalar(select(db.DemoOrder).where(db.DemoOrder.user_id==user.id,
                                                  db.DemoOrder.contract_id==contract_id))
        if owned is None:
            raise HTTPException(404,'Contrato não encontrado para esta conta')
    try:
        return await demo_contract_status(token,user.deriv_account_id,contract_id)
    except DemoTradingError as e:
        raise HTTPException(502,str(e)) from None
