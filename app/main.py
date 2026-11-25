"""NEXORA IA. Official Deriv DEMO orders and read-only AutoPilot research."""
import asyncio, json, logging, os, secrets, re, time
from math import isfinite
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
from .public_urls import origin_for_request, checkout_return_origin, nexora_referral_origin, canonical_origin
from .stripe_billing import PLANS, billing_enabled, stripe_expected_livemode, create_checkout, stripe_signature_valid, apply_paid_checkout, plan_can_purchase, test_plan_enabled
from .automation_markets import GROUPS as AUTOMATION_GROUPS, instruments_by_group, featured_instruments_by_group
from .access import (access_control_enabled, access_status, access_status_for_grant,
                     start_trial_on_first_robot_start, register_first_login,
                     referral_report, trial_duration_hours, utc)
from .demo import (DemoTradingError, DemoPreBuyDeadlineExceeded, demo_account_balance,
                   demo_contract_status, demo_buy, demo_proposal, demo_expiry_quotes,
                   prepare_demo_buy_socket)
from .demo_ledger import (OrderGuardError, reserve, public_order, confirmed, unknown,
                          settle, link_recovered_contract, mark_broker_absent)
from . import real_runtime
from .real_broker import RealExecutionError, real_account_balance, real_contract_status
from .order_recovery import (RecoverySnapshotError, account_recovery_snapshot,
                             classify_unidentified_order)
from .autopilot import analyze, quote_assessment
from .engine import Engine, Settings
from .market import MarketError, discover_symbols, discover_contracts, indicative_proposal, public_feed_messages
from .strategy_lab import run_lab
from .single_asset_study import run_single_asset_study
from .research_persistence import (research_loop, latest_study, latest_multitimeframe_study,
    latest_pullback_study, HistoricalResearchReport, RESEARCH_KEY,
    MULTI_RESEARCH_KEY, PULLBACK_KEY)  # read-only research
from .m1_history import m1_collect_loop, history_status
from .asset_scanner import scan_assets, scan_intelligence_markets, scan_intelligence_context
from .trade_selector_v2 import (select_demo_signal, last_scan_report, has_pending_signals,
                                active_market, catalogue, supported, fetch_closed_timeframe,
                                SIGNAL_PROFILE)
from .progression import next_entry, ProgressionHalt, NextEntry
from .expiry_policy import proposal_durations
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
research_worker_task = None
m1_research_task = None
real_task = None
real_reconciliation_task = None
# Runtime state is volatile; explicit DEMO resume intent is persisted separately.
autopilot_enabled: set[int] = set()
autopilot_last_attempt: dict[int,float] = {}
v2_last_scan: dict[int,float] = {}
v2_gate_last_log: dict[tuple[int,str],float] = {}
autopilot_reasons: dict[int,str] = {}
# Read-only progress snapshots. Only real scanner callbacks update symbols.
automation_scan_progress: dict[int,dict] = {}

def record_automation_scan(uid:int, event:str, symbol:str|None, total:int, reason:str|None=None):
    """In-memory diagnostics of real market checks, not synthetic activity."""
    if uid not in autopilot_enabled:
        return
    now=datetime.now(timezone.utc).isoformat()
    state=automation_scan_progress.setdefault(uid,{
        'stage':'waiting','last_symbol':None,'active_symbol':None,
        'checked':0,'total':0,'last_checked_at':None,'last_scan_at':None,
        'cycle':0,'gale':0,'reason_counts':{},'last_reason':None,
        'last_rejected_symbol':None,'last_rejected_at':None})
    if event=='start':
        state.update(stage='scanning',checked=0,total=total,
                     active_symbol=None,last_scan_at=now,
                     reason_counts={},last_reason=None,last_rejected_symbol=None)
    elif event=='checking':
        state.update(stage='scanning',active_symbol=symbol)
    elif event=='checked':
        state.update(last_symbol=symbol,last_checked_at=now,
                     checked=min(state.get('checked',0)+1,total))
        if reason:
            counts=state.setdefault('reason_counts',{})
            counts[reason]=counts.get(reason,0)+1
            if reason!='sinal_confirmado':
                state.update(last_reason=reason,last_rejected_symbol=symbol,
                             last_rejected_at=now)
    elif event=='window':
        # Outside closed-candle entry window: no market scan took place.
        if not state.get('last_scan_at'):
            state.update(stage='waiting',last_reason=reason)
    elif event=='cached':
        if state.get('stage')=='scanning':
            state.update(stage='waiting',active_symbol=None)
    elif event=='timeout':
        state.update(stage='waiting',active_symbol=None,last_reason='tempo_esgotado')
        counts=state.setdefault('reason_counts',{})
        counts['tempo_esgotado']=counts.get('tempo_esgotado',0)+1
    elif event=='signal':
        state.update(stage='signal',last_symbol=symbol,last_checked_at=now,
                     active_symbol=None)

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
            async with websockets.connect(PUBLIC_WS,ping_interval=20,ping_timeout=20,
                                          open_timeout=10,close_timeout=3,
                                          max_size=1_500_000) as ws:
                await ws.send(json.dumps({'ticks_history':SYMBOL,'count':100,'end':'latest','style':'ticks'}))
                await ws.send(json.dumps({'ticks':SYMBOL,'subscribe':1}))
                async with lock:
                    # A successful handshake alone is not a verified price feed.
                    market_connected=False
                    market_last_tick=0
                    last_market_reason='Canal aberto; aguardando primeira cotação'
                    for engine in runtimes.values():
                        engine.connected=False;engine.prices.clear()
                    recent_prices=[]
                async for raw in public_feed_messages(ws):
                    m=json.loads(raw)
                    if m.get('error'):raise RuntimeError(str(m['error'].get('message','Erro da Deriv')))
                    if m.get('msg_type')=='history':
                        ps=[float(p) for p in m.get('history',{}).get('prices',[])]
                        async with lock:
                            recent_prices=(recent_prices+ps)[-200:]
                            for engine in runtimes.values():engine.prices.extend(ps)
                    elif m.get('msg_type')=='tick':
                        p=float(m['tick']['quote'])
                        async with lock:
                            market_last_tick=asyncio.get_running_loop().time()
                            market_connected=True
                            last_market_reason='Cotações públicas conectadas'
                            delay=2
                            recent_prices=(recent_prices+[p])[-200:]
                            for uid,engine in list(runtimes.items()):
                                engine.connected=True
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
                market_connected=False;market_last_tick=0
                last_market_reason='Conexão interrompida'
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
    automation_scan_progress.pop(uid,None)
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
                        if batch and not v2_enabled():
                            # Old progression is never evaluated for PULSO V2.
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
    """Choose the next official-ledger progression in one EXCLUSIVE mode.

    Martingale uses independent per-symbol sequences and repeats the last
    confirmed direction on that same symbol at the next candle boundary.
    Cycle-only mode uses the chronological run ledger across ALL symbols:
    each LOSS advances the stake, and the next entry needs a NEW signal.
    Never infer settlement from a timer or count CANCELLED pre-buy orders.
    """
    if not batch:
        return None,None
    run=sess.get(db.RobotRun,user.id)
    if not run:
        raise ProgressionHalt('Reative o AutoPilot para iniciar uma sequência DEMO')
    if plan and plan.requested_martingale and plan.cycles_limit:
        raise ProgressionHalt('Martingale e ciclos são configurações alternativas')
    after=max(batch.after_order_id,run.after_order_id)
    historical=sorted((o for o in orders if o.id>after and o.state!='CANCELLED'),
                      key=lambda o:o.id)
    if not historical:
        return None,None
    ids=[o.id for o in historical]
    attributes={a.order_id:a for a in sess.scalars(select(db.AutoPilotAttribution).where(
        db.AutoPilotAttribution.user_id==user.id,
        db.AutoPilotAttribution.order_id.in_(ids))).all()}
    for order in historical:
        if order.id not in attributes:
            raise ProgressionHalt('Contrato sem atribuição automática na avaliação 2.0')

    def rows_for(group):
        return [{'id':o.id,'state':o.state,'pnl':o.pnl,
                 'direction':o.direction,'symbol':o.symbol,'stake':o.stake,
                 'strategy':attributes[o.id].strategy} for o in group]

    if plan and plan.cycles_limit:
        # One cycle-only sequence spans ANY asset. It cannot be combined with
        # parallel open orders, which would make the next-cycle stake ambiguous.
        if any(o.state in ('PENDING','CONFIRMED','UNKNOWN') for o in historical):
            raise ProgressionHalt('Contrato anterior não liquidado; aguardando reconciliação')
        entry=next_entry(base=user.stake,gale_enabled=False,gales=0,
                         gale_multiplier='1',cycles=plan.cycles_limit,
                         cycles_enabled=True,
                         cycle_multiplier=cycle_cfg.multiplier if cycle_cfg else '2',
                         orders=rows_for(historical))
        # A completed loss advances to a new cycle but never reuses the
        # previous signal. Retry remains FALSE (gale=0).
        return entry,historical[-1]

    if not plan or not plan.requested_martingale:
        return None,None
    groups={}
    for order in historical:
        groups.setdefault(order.symbol,[]).append(order)
    owed=[]
    for symbol,group in groups.items():
        if any(o.state in ('PENDING','CONFIRMED','UNKNOWN') for o in group):
            continue
        try:
            entry=next_entry(base=user.stake,gale_enabled=True,
                             gales=plan.gale_count,
                             gale_multiplier=plan.requested_multiplier,
                             cycles=None,cycles_enabled=False,
                             cycle_multiplier='1',orders=rows_for(group))
        except ProgressionHalt:
            raise
        if entry.retry:
            owed.append((group[-1],entry))
    if not owed:
        return None,None
    last,entry=min(owed,key=lambda pair:(
        pair[0].updated_at or pair[0].created_at,pair[0].id))
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


def martingale_next_candle_window(settled_at, minutes, *, now=None, grace_seconds=8):
    """Only permit a Martingale in a subsequent candle of the same expiry.

    Return (target_epoch, is_open). The target is NEVER the same candle as
    broker settlement, including when settlement arrives at its opening.
    """
    if minutes not in (1,3,5,15):
        raise ValueError('Expiração de recuperação inválida')
    if not settled_at:
        return None,False
    if settled_at.tzinfo is None:
        settled_at=settled_at.replace(tzinfo=timezone.utc)
    ts=time.time() if now is None else float(now)
    period=minutes*60
    settled=settled_at.timestamp()
    target=(int(settled//period)+1)*period
    if ts>target+grace_seconds:
        # Missed a boundary: only retry on the next one, never mid-candle.
        target=max(target+period,(int(ts//period)+1)*period)
    return target,target<=ts<=target+grace_seconds


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
                # Enforce settled-result stops on EVERY worker tick, even when
                # the scanner finds no signal. Previously an armed robot could
                # remain green after reaching Stop Win / Stop Loss indefinitely.
                boundary=latest_risk_session(sess,uid)
                after_id=boundary.after_order_id if boundary else 0
                try:
                    settled_pnl=sum((
                        Decimal(str(o.pnl)) for o in orders
                        if o.state=='SETTLED' and o.pnl is not None
                        and (o.id or 0)>after_id),Decimal('0'))
                    stop_win=Decimal(str(user.stop_win))
                    stop_loss=Decimal(str(user.stop_loss))
                    if not all(v.is_finite() for v in (settled_pnl,stop_win,stop_loss)):
                        raise InvalidOperation()
                except (InvalidOperation,ValueError,TypeError):
                    disarm_autopilot(uid,'Resultado ou limite de risco inválido; robô desligado')
                    continue
                if stop_win>0 and settled_pnl>=stop_win:
                    disarm_autopilot(uid,'Stop Win atingido; robô desligado')
                    continue
                if stop_loss>0 and settled_pnl<=-stop_loss:
                    disarm_autopilot(uid,'Stop Loss atingido; robô desligado')
                    continue
                plan=sess.get(db.RobotPlan,uid)
                cycle_cfg=sess.get(db.RobotCycleParams,uid)
                if plan and plan.requested_martingale and plan.cycles_limit:
                    disarm_autopilot(uid,'Escolha apenas Martingale OU ciclos nas configurações; operações existentes continuam em reconciliação')
                    continue
                batch=sess.get(db.PilotValidationBatch,uid)
                # PULSO V2 replaces the legacy scanner for every manually
                # armed DEMO evaluation; no extra strategy-selection button.
                pulso_active=bool(batch and v2_enabled())
                profile=sess.get(db.AutomationProfile,uid) if pulso_active else None
                if not batch and not connected:
                    continue
                # The goal of 50 trades is informational, NOT an execution cap.
                # Risk stops and broker-uncertainty safeguards remain mandatory.
                try:
                    if pulso_active:
                        # Contracts with uncertain outcomes stay locked to
                        # their own symbol. The ledger's durable unique slot
                        # prevents duplicate buys while other symbols progress.
                        active=[o for o in orders if o.state in ('PENDING','CONFIRMED','UNKNOWN')]
                        entry=last=None
                        if plan and (plan.requested_martingale or plan.cycles_limit):
                            entry,last=v2_symbol_recovery(sess,user,orders,plan,cycle_cfg,batch)
                        if entry is None:
                            entry=NextEntry(stake=Decimal(str(user.stake)),cycle=0,gale=0,retry=False)
                    elif batch:
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
                if pulso_active:
                    tracker=automation_scan_progress.setdefault(uid,{})
                    tracker['cycle']=int(entry.cycle or 0)
                    tracker['gale']=int(entry.gale or 0)
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
                    if batch and pulso_active:
                        target_open,ready=martingale_next_candle_window(
                            settlement,last.duration_minutes,now=time.time())
                        if not ready:
                            autopilot_reasons[uid]=(
                                'Martingale M'+str(last.duration_minutes)+
                                ': aguardando abertura da próxima vela após LOSS oficial')
                            log_v2_gate(uid,'MARTINGALE_NEXT_CANDLE_WAIT')
                            continue
                    if batch and now-autopilot_last_attempt.get(uid,-1e12)<(1 if pulso_active else 5):
                        log_v2_gate(uid,'RECOVERY_COOLDOWN')
                        continue
                    # Warm the authenticated DEMO socket while there is time.
                    # Otherwise Martingale follows a cold OTP+WS connection and
                    # commonly misses the three-second M5 opening window.
                    if (batch and not pulso_active and SIGNAL_PROFILE=='next_open_v24'
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
                    if batch: log_v2_gate(uid,'MARTINGALE_PENDING')
                    candidate={'strategy':entry.strategy or 'recuperacao',
                        'direction':entry.direction,
                        'symbol':entry.symbol or SYMBOL,
                        'minutes':last.duration_minutes if last else 5,
                        'reason':'Continuação controlada: ciclo '+str(entry.cycle)+
                                 ', martingale '+str(entry.gale)}
                else:
                    if batch and entry.cycle>0 and last is not None:
                        autopilot_reasons[uid]='Ciclo '+str(entry.cycle)+' aguardando próximo sinal confirmado · '+str(entry.stake)+' USD'
                        log_v2_gate(uid,'CYCLE_WAITING_SIGNAL')
                    if not batch and now-autopilot_last_attempt.get(uid,-1e12)<60:
                        continue
                    if batch and pulso_active:
                        if now-autopilot_last_attempt.get(uid,-1e12)<8:
                            continue
                        allowed_symbols=None
                        if profile is not None:
                            if (profile.account_mode!='demo'
                                or profile.market not in (*AUTOMATION_GROUPS,'volatility')
                                or profile.strategy not in ('pulso_v2','trend','rsi')
                                or profile.expiry_minutes not in (0,1,3,5,15)
                                or profile.news_filter):
                                autopilot_reasons[uid]='Configuração incompatível ou filtro de notícias indisponível; sem novas entradas'
                                continue
                            try:
                                allowed_symbols=json.loads(profile.symbols_json)
                            except (TypeError,ValueError):
                                autopilot_reasons[uid]='Configuração de ativos inválida'
                                continue
                            if not allowed_symbols:
                                autopilot_reasons[uid]='Nenhum ativo habilitado'
                                continue
                        # Expiração automática também vale para perfis antigos, sem
                        # exigir que o usuário refaça a configuração.
                        if profile is not None:
                            profile.expiry_minutes=0
                        try:
                            from .automation_signals import confirmed_automation_signal
                            candidate=await confirmed_automation_signal(
                                profile,
                                scan_event=lambda event,symbol,total,reason=None: record_automation_scan(
                                    uid,event,symbol,total,reason),
                                excluded_symbols=[o.symbol for o in orders
                                                  if o.state in ('PENDING','CONFIRMED','UNKNOWN')])
                        except (MarketError,ValueError,TimeoutError,OSError) as exc:
                            state=automation_scan_progress.get(uid)
                            if state: state.update(stage='waiting',active_symbol=None,last_reason='dados_indisponiveis')
                            autopilot_reasons[uid]='PULSO V2: cotações DEMO indisponíveis; sem compra'
                            log.warning('PULSO live data unavailable: %s',type(exc).__name__)
                            continue
                        if candidate is None:
                            state=automation_scan_progress.get(uid)
                            if state and state.get('stage')=='scanning':
                                state.update(stage='waiting',active_symbol=None)
                            if state and state.get('last_scan_at') and state.get('checked')==state.get('total') and state.get('total'):
                                counts=state.get('reason_counts',{})
                                log_v2_gate(uid,'SCAN_REASONS',detail=' '.join(
                                    k+'='+str(v) for k,v in sorted(counts.items())))
                            autopilot_reasons[uid]='Aguardando sinal confirmado na estratégia selecionada'
                            continue
                    elif batch:
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
                    # Expiration AUTO evaluates the actual broker offers rather
                    # than picking a duration at random. No eligible offer means no buy.
                    selected_ratio=(Decimal('1')+Decimal(str(profile.min_payout))/Decimal('100')
                                    if pulso_active and profile is not None else Decimal('1.50'))
                    auto_expiry=bool(pulso_active and profile is not None)
                    durations=proposal_durations(
                        profile.market if auto_expiry else '',
                        candidate.get('expiry_options',(candidate['minutes'],)),
                        automatic=auto_expiry)
                    if not durations:
                        autopilot_reasons[uid]='Nenhuma expiração candidata válida; nenhuma proposta enviada'
                        log_v2_gate(uid,'NO_CANDIDATE_EXPIRY',detail='symbol='+str(candidate['symbol']))
                        continue
                    best_offer=None
                    cutoff=(candidate.get('last_bar_epoch',0)+60+12
                            if pulso_active and not entry.retry else None)
                    checked_expiry=[]
                    rejected_expiry=[]
                    quote_failure="none"
                    quote_elapsed_start=time.monotonic()
                    timeout=max(0.0,min(6.0,cutoff-time.time()-0.7)) if cutoff else 6.0
                    if timeout>0:
                        try:
                            response=await asyncio.wait_for(demo_expiry_quotes(
                                token,user.deriv_account_id,
                                symbol=candidate['symbol'],contract_type=candidate['direction'],
                                stake=str(entry.stake),durations=durations),timeout=timeout)
                            for duration,proposal in response['offers'].items():
                                checked_expiry.append(duration)
                                try:
                                    ask=Decimal(str(proposal['ask_price']))
                                    payout=Decimal(str(proposal['payout']))
                                    if (not ask.is_finite() or not payout.is_finite()
                                        or ask<=0 or ask>entry.stake or payout<ask*selected_ratio):
                                        rejected_expiry.append(duration)
                                        continue
                                    ratio=payout/ask
                                    if best_offer is None or (ratio,duration)>(best_offer[0],best_offer[1]):
                                        best_offer=(ratio,duration)
                                except (InvalidOperation,ValueError,KeyError,TypeError):
                                    rejected_expiry.append(duration)
                            for duration,code in response['failures'].items():
                                log.info('NEXORA expiry rejected by Deriv: symbol=%s duration=%s deriv_code=%s',
                                         candidate['symbol'],duration,code)
                        except asyncio.TimeoutError:
                            quote_failure="timeout"
                        except (DemoTradingError,OSError,ValueError,TypeError) as exc:
                            quote_failure=type(exc).__name__
                    else:
                        quote_failure="expired_before_quotes"
                    elapsed=round(time.monotonic()-quote_elapsed_start,3)
                    log.warning('NEXORA expiry scan: symbol=%s candidates=%s quoted=%s rejected_payout=%s chosen=%s failure=%s elapsed_s=%s',
                                candidate['symbol'],durations,checked_expiry,rejected_expiry,
                                best_offer[1] if best_offer else None,quote_failure,elapsed)
                    if best_offer is None:
                        if quote_failure=='timeout':
                            autopilot_reasons[uid]='Deriv demorou a responder às propostas de expiração; entrada descartada com segurança'
                        elif quote_failure!='none':
                            autopilot_reasons[uid]='Falha na consulta de expiração: '+quote_failure
                        else:
                            autopilot_reasons[uid]='Nenhuma expiração atendeu ao payout mínimo ou à disponibilidade na Deriv'
                        log_v2_gate(uid,'NO_VALID_EXPIRY',detail='symbol='+str(candidate['symbol'])+' reason='+quote_failure)
                        continue
                    candidate['minutes']=best_offer[1]
                    if auto_expiry:
                        # Expiry already lives in DemoOrder.duration_minutes and reason.
                        # Never append it to strategy: the DB identifier is VARCHAR(32).
                        # AUTO strategy IDs can reach that limit before the suffix.
                        candidate['reason']=candidate['reason']+' · AUTO M'+str(best_offer[1])
                        state=automation_scan_progress.get(uid)
                        if state: state['selected_expiry']=best_offer[1]
                    log.info('NEXORA expiry selected: symbol=%s minutes=%s quoted_payout_ratio=%.3f',
                             candidate['symbol'],best_offer[1],float(best_offer[0]))
            if uid not in autopilot_enabled:
                continue
            deadline_for_buy=(candidate['last_bar_epoch']+60+12
                              if pulso_active and not entry.retry else None)
            if pulso_active and entry.retry:
                # This is a deadline for an existing confirmed candle only;
                # never extend it to an arbitrary mid-candle instant.
                target_open,ready=martingale_next_candle_window(
                    last.updated_at,last.duration_minutes,now=time.time())
                if not ready:
                    autopilot_reasons[uid]='Martingale: janela da próxima vela encerrada'
                    continue
                deadline_for_buy=target_open+8
            timed_v24=(bool(batch) and not pulso_active and SIGNAL_PROFILE=='next_open_v24'
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
                                 'v2':bool(batch),'pulso_v2':pulso_active,
                                 'min_payout_ratio':str(Decimal('1')+Decimal(str(profile.min_payout))/Decimal('100')) if pulso_active and profile is not None else '1.50',
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


def _reconciliation_due(order, now=None):
    """Skip far-from-expiry contracts; check officially sold/unknown ones ASAP.

    A short early margin catches broker closures near their expected expiry.
    Never infer a WIN/LOSS from time alone.
    """
    if order.state=='UNKNOWN':
        return True
    when=order.broker_purchase_at or order.created_at
    if when is None:
        return True
    if when.tzinfo is None:
        when=when.replace(tzinfo=timezone.utc)
    current=now or datetime.now(timezone.utc)
    return (current-when).total_seconds()>=order.duration_minutes*60-3


async def _reconcile_one_order(order_id):
    # Queries to Deriv are read-only and can be made independently per
    # contract; only database settlement requires the shared reconcile lock.
    with db.SessionLocal() as s:
        order=s.get(db.DemoOrder,order_id)
        if not order or order.state not in ('CONFIRMED','UNKNOWN') or not order.contract_id:
            return
        if not _reconciliation_due(order):
            return
        user=s.get(db.User,order.user_id)
        if not user:
            return
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
        return
    try:
        receipt=await demo_contract_status(token,account_id,contract_id)
        async with reconcile_lock:
            with db.SessionLocal() as s:
                order=s.get(db.DemoOrder,order_id)
                if not order or order.state not in ('CONFIRMED','UNKNOWN'):
                    return
                try:
                    settled=settle(s,order,receipt.get('contract',{}))
                except OrderGuardError as exc:
                    log.warning('Demo reconciliation rejected: order=%s error=%s',order_id,type(exc).__name__)
                    return
                if settled:
                    log.info('Demo contract settlement recorded from Deriv: order=%s',order_id)
    except DemoTradingError as exc:
        log.warning('Demo reconciliation unavailable: order=%s error=%s',order_id,type(exc).__name__)


async def reconcile_confirmed_orders_once():
    """Poll due contracts concurrently, without guessing any outcomes.

    Limit read-only broker status requests to four simultaneous sockets.
    Reserve/confirm/unknown ledger protections are unchanged.
    """
    with db.SessionLocal() as s:
        rows=s.scalars(select(db.DemoOrder).where(
            db.DemoOrder.state.in_(('CONFIRMED','UNKNOWN')),
            db.DemoOrder.contract_id.is_not(None))
            .order_by(db.DemoOrder.id.desc()).limit(100)).all()
        due_ids=[o.id for o in rows if _reconciliation_due(o)]
    sem=asyncio.Semaphore(4)
    async def process(order_id):
        async with sem:
            try:
                await _reconcile_one_order(order_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('Demo reconciliation failed for order=%s',order_id)
    if due_ids:
        await asyncio.gather(*(process(order_id) for order_id in due_ids))


async def reconcile_confirmed_loop():
    while True:
        try:
            await reconcile_confirmed_orders_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('Demo reconciliation loop failed')
        # Poll near-expiry/unknown contracts promptly, without opening
        # repeated broker sockets for contracts that cannot have expired yet.
        await asyncio.sleep(1)


@asynccontextmanager
async def lifespan(app):
    global market_task, reconcile_task, autopilot_task, research_worker_task, m1_research_task, real_task, real_reconciliation_task
    if os.getenv('NEXORA_PUBLIC_URL','').startswith('https://') and db.DB_URL.startswith('sqlite:'):
        raise RuntimeError('Não usar SQLite efêmero em implantação pública: configure DATABASE_URL de Postgres')
    db.init_db()
    restore_demo_autopilot_consent()
    market_task=asyncio.create_task(feed())
    reconcile_task=asyncio.create_task(reconcile_confirmed_loop())
    autopilot_task=asyncio.create_task(autopilot_loop())
    market_selfcheck_task=asyncio.create_task(public_market_selfcheck_once())
    research_worker_task=asyncio.create_task(research_loop(),name='research-1hz100v-daily')
    m1_research_task=asyncio.create_task(m1_collect_loop(),name='research-1hz100v-m1-history')
    real_task=asyncio.create_task(real_runtime.real_loop(),name='real-autopilot-opt-in')
    real_reconciliation_task=asyncio.create_task(real_runtime.reconcile_real_loop(),name='real-contract-reconcile')
    yield
    autopilot_enabled.clear()
    real_runtime.running.clear()
    tasks=[task for task in (market_task,reconcile_task,autopilot_task,market_selfcheck_task,research_worker_task,m1_research_task,real_task,real_reconciliation_task) if task]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks,return_exceptions=True)

app=FastAPI(title='NEXORA IA',version=VERSION,lifespan=lifespan)

# Bounded, read-only performance diagnostics: no account IDs, payloads, cookies,
# URL queries or financial values may be included in timing logs.
_REVIEW_READ_PATHS=frozenset((
    '/api/automation/profile',
    '/api/onboarding/config',
    '/api/automation/catalogue',
))

@app.middleware('http')
async def headers(request:Request, call_next):
    # Only the three read-only review endpoints are timed. Never log identities,
    # request bodies or catalogue query parameters.
    review_path=request.url.path
    review_start=(time.perf_counter()
                  if request.method=='GET' and review_path in _REVIEW_READ_PATHS else None)
    # Same-origin browser calls; origin check on mutating requests protects session cookies from CSRF.
    if request.method in ('POST','PUT','PATCH','DELETE'):
        origin=request.headers.get('origin')
        if origin:
            expected=origin_for_request(request)
            if not expected or origin.rstrip('/')!=expected:
                return JSONResponse({'detail':'Origem não autorizada'},403)
    response=await call_next(request)
    if review_start is not None:
        elapsed_ms=(time.perf_counter()-review_start)*1000
        response.headers['Server-Timing']=f'review_read;dur={elapsed_ms:.1f}'
        if elapsed_ms>=1000:
            log.warning('slow review read path=%s elapsed_ms=%.1f status=%d',
                        review_path,elapsed_ms,response.status_code)
    response.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer',
        'X-Frame-Options':'DENY','Content-Security-Policy':"default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'"})
    return response

@app.get('/')
async def index(): return FileResponse(Path(__file__).parent/'index.html')

@app.get('/configuracao-educativa', include_in_schema=False)
async def configuracao_educativa():
    return FileResponse(Path(__file__).parent/'configuracao_educativa.html', media_type='text/html')

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
            'manual_demo_purchase_enabled':enabled,'real_orders_enabled':real_runtime.real_available(),
            'execution_modes':['demo']+(['real'] if real_runtime.real_available() else [])}

@app.get('/api/public/offer')
async def public_offer():
    """Public, non-sensitive source of truth for the trial landing page."""
    return {'trial_hours':trial_duration_hours(),
            'trial_starts':'first_verified_deriv_login',
            'referral_bonus_hours':24}


def oauth_recovery_redirect(nexora_sid:str|None, *, reason:str='expired'):
    """Treat repeated callbacks with a live login as a successful existing session.

    OAuth codes and state are single-use. Browser reload/back navigation can
    repeat their callback after login succeeded; never display a raw HTTP 400
    or exchange the authorization code again. Unknown callbacks must NOT
    create sessions, and unauthenticated visitors must log in afresh.
    """
    if nexora_sid:
        try:
            active_session(None,nexora_sid)
        except HTTPException:
            pass
        else:
            response=RedirectResponse('/?login=existing',status_code=303)
            response.delete_cookie('nexora_state',path='/')
            return response
    reason=reason if reason in ('expired','cancelled','failed') else 'expired'
    response=RedirectResponse('/?login='+reason,status_code=303)
    response.delete_cookie('nexora_state',path='/')
    return response


@app.get('/auth/deriv/start')
async def login_start(request:Request,ref:str|None=None,register:bool=False):
    # OAuth must begin on the same host where Deriv will return the browser.
    # During migration, route visitors to the configured canonical host BEFORE
    # creating the PKCE state cookie. This preserves legacy invite links.
    current=origin_for_request(request)
    canonical=canonical_origin()
    if not current or not canonical:
        raise HTTPException(503,'Endereço de login não configurado')
    if current!=canonical:
        params=[]
        if ref and re.fullmatch(r'[A-Za-z0-9_-]{8,32}',ref):
            params.append('ref='+ref)
        if register:
            params.append('register=true')
        suffix=('?'+'&'.join(params)) if params else ''
        return RedirectResponse(canonical+'/auth/deriv/start'+suffix,status_code=302)
    state,verifier=make_login()
    if ref and not re.fullmatch(r'[A-Za-z0-9_-]{8,32}',ref):
        ref=None
    with db.SessionLocal() as s:
        # Keep single-use PKCE verifiers only while an OAuth authorization
        # remains usable. Expired flows must not accumulate in PostgreSQL.
        s.execute(delete(db.LoginFlow).where(
            db.LoginFlow.created_at < db.utcnow()-timedelta(minutes=15)))
        s.add(db.LoginFlow(state_hash=hashsecret(state),
                           verifier_encrypted=encrypt(verifier),referral_code=ref))
        s.commit()
    response=RedirectResponse(build_authorize_url(state,verifier,register=register),302)
    response.set_cookie('nexora_state',state,max_age=600,**cookie_options())
    return response

@app.get('/auth/deriv/callback')
async def callback(code:str|None=None,state:str|None=None,error:str|None=None,
                   nexora_state:str|None=Cookie(default=None),
                   nexora_sid:str|None=Cookie(default=None)):
    if not state or not nexora_state or not secrets.compare_digest(state,nexora_state):
        return oauth_recovery_redirect(nexora_sid)
    with db.SessionLocal() as s:
        obj=s.get(db.LoginFlow,hashsecret(state))
        if obj is None:
            return oauth_recovery_redirect(nexora_sid)
        age=db.utcnow()- (obj.created_at.replace(tzinfo=timezone.utc) if obj.created_at.tzinfo is None else obj.created_at)
        if age.total_seconds()>600 or age.total_seconds() < -30:
            s.delete(obj)
            s.commit()
            return oauth_recovery_redirect(nexora_sid)
        verifier=decrypt(obj.verifier_encrypted)
        referral_code=obj.referral_code
        s.delete(obj);s.commit()
    if error:return oauth_recovery_redirect(nexora_sid,reason='cancelled')
    if not code:return oauth_recovery_redirect(nexora_sid)
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            token_resp=await client.post(TOKEN_URL,data={'grant_type':'authorization_code','client_id':CLIENT_ID,
                'code':code,'code_verifier':verifier,'redirect_uri':REDIRECT})
            token_resp.raise_for_status()
            token_data=token_resp.json();token=token_data['access_token'];ttl=int(token_data['expires_in'])
            accounts_resp=await client.get('https://api.derivws.com/trading/v1/options/accounts',
                headers={'Authorization':f'Bearer {token}'})
            accounts_resp.raise_for_status();payload=accounts_resp.json()
    except (httpx.HTTPError,ValueError,KeyError) as exc:
        log.warning('Deriv OAuth callback unavailable: %s',type(exc).__name__)
        return oauth_recovery_redirect(nexora_sid,reason='failed')
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
            'real_enabled':real_runtime.real_available(),'is_admin':False}

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
            'real_accounts':found,'real_trading_enabled':real_runtime.real_available(),
            'execution':'DERIV_REAL_OPT_IN' if real_runtime.real_available() else 'DERIV_DEMO_ONLY'}


@app.get('/api/real/orders')
async def real_orders_route(request:Request,nexora_sid:str|None=Cookie(default=None)):
    """Own-account REAL contracts. Never mix with the DEMO trade ledger."""
    user=active_user(request,nexora_sid)
    from .real_ledger import public_real_order
    with db.SessionLocal() as sess:
        orders=sess.scalars(select(db.RealOrder).where(
            db.RealOrder.user_id==user.id).order_by(
            db.RealOrder.id.desc()).limit(100)).all()
        return {'items':[public_real_order(o) for o in orders],
                'account_mode':'real','source':'DERIV_REAL_ORDERS'}


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
                # Logout revokes REAL consent too; never leave a live-money
                # worker running under an authentication session being deleted.
                async with real_runtime.user_lock(session.user_id):
                    real_runtime.disarm_real(session.user_id,
                        'Sessão encerrada; execução REAL desligada')
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


class AutomationProfileRequest(BaseModel):
    """Explicit user choices; not an instruction to place a buy."""
    account_mode:str='demo'
    market:str='volatility'
    symbols:list[str]=[]
    strategy:str='pulso_v2'
    expiry_minutes:int=0
    news_filter:bool=False
    news_minutes_before:int=30
    news_minutes_after:int=30
    max_daily_orders:int=0
    # Legacy field accepted for older clients; strategy rules now own their filters.
    indicators:list[str]=[]
    direction:str='AUTO'
    real_account_id:str|None=None
    min_payout_pct:int=88
    preset_mode:str='custom'


def automation_profile_result(profile):
    if profile is None:
        return {
            'account_mode':'demo','market':'volatility','symbols':[],
            'strategy':'pulso_v2','expiry_minutes':0,
            'news_filter':False,'news_minutes_before':30,
            'news_minutes_after':30,'max_daily_orders':0,
            'indicators':[],'direction':'AUTO','real_account_id':None,
            'min_payout_pct':88,'preset_mode':'custom',
            'configured':False,'execution':'DERIV_DEMO_ONLY'
        }
    return {
        'account_mode':profile.account_mode,'market':profile.market,
        'symbols':json.loads(profile.symbols_json),
        'strategy':profile.strategy,'expiry_minutes':0,
        'news_filter':profile.news_filter,
        'news_minutes_before':profile.news_minutes_before,
        'news_minutes_after':profile.news_minutes_after,
        'max_daily_orders':0,
        # Do not expose stale choices from profiles created by older panels.
        'indicators':[],
        'direction':profile.direction,
        'real_account_id':profile.real_account_id,
        'min_payout_pct':int(Decimal(profile.min_payout)),
        'preset_mode':profile.preset_mode,
        'configured':True,
        'execution':'DERIV_REAL_CONFIGURED' if profile.account_mode=='real' else 'DERIV_DEMO_ONLY'
    }


@app.get('/api/automation/profile')
async def get_automation_profile(request:Request,nexora_sid:str|None=Cookie(default=None)):
    user=active_user(request,nexora_sid)
    with db.SessionLocal() as session:
        return automation_profile_result(session.get(db.AutomationProfile,user.id))


@app.post('/api/automation/profile')
async def save_automation_profile(request:Request,payload:AutomationProfileRequest,
                                  nexora_sid:str|None=Cookie(default=None)):
    user,token=active_session(request,nexora_sid)
    if payload.account_mode not in ('demo','real'):
        raise HTTPException(422,'Modo de conta inválido')
    if payload.market not in (*AUTOMATION_GROUPS, 'volatility') or payload.strategy not in ('pulso_v2','trend','rsi'):
        raise HTTPException(422,'Mercado ou estratégia inválidos')
    if payload.expiry_minutes not in (0,1,3,5,15):
        raise HTTPException(422,'Expiração inválida')
    if not 1<=len(payload.symbols)<=5 or len(payload.symbols)!=len(set(payload.symbols)):
        raise HTTPException(422,'Selecione de 1 a 5 ativos diferentes')
    if any(not re.fullmatch(r'[A-Za-z0-9_]{2,30}',x) for x in payload.symbols):
        raise HTTPException(422,'Símbolo inválido')
    if payload.news_minutes_before not in (0,15,30,60) or payload.news_minutes_after not in (0,15,30,60):
        raise HTTPException(422,'Janela de notícias inválida')
    if payload.direction not in ('AUTO','CALL','PUT'):
        raise HTTPException(422,'Direção inválida')
    if not 0<=payload.min_payout_pct<=200:
        raise HTTPException(422,'Payout mínimo fora do intervalo')
    if payload.preset_mode not in ('conservative','aggressive','custom'):
        raise HTTPException(422,'Perfil de configuração inválido')
    # Validate legacy values for compatibility, but never persist or apply them.
    indicator_options={'EMA','SMA','RSI','MACD','BOLLINGER','ATR','ADX','STOCHASTIC','SUPPORT_RESISTANCE','CANDLE_PATTERNS'}
    if len(payload.indicators)!=len(set(payload.indicators)) or not set(payload.indicators).issubset(indicator_options):
        raise HTTPException(422,'Seleção de indicadores inválida')
    if payload.account_mode=='real':
        if not payload.real_account_id or not re.fullmatch(r'[A-Za-z0-9]{2,100}',payload.real_account_id):
            raise HTTPException(422,'Selecione uma conta REAL vinculada à Deriv')
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                res=await client.get('https://api.derivws.com/trading/v1/options/accounts',
                    headers={'Authorization':f'Bearer {token}'})
                res.raise_for_status()
                payload_accounts=res.json()
        except (httpx.HTTPError,ValueError):
            raise HTTPException(503,'Não foi possível consultar as contas vinculadas à Deriv') from None
        from .auth_helpers import extract_accounts,real_accounts
        owned={a['account_id'] for a in real_accounts(extract_accounts(payload_accounts))}
        if payload.real_account_id not in owned:
            raise HTTPException(403,'Conta REAL não pertence à sessão autenticada')
    # Validate membership in the actual broker catalog, not a fabricated list.
    try:
        instruments=await featured_instruments_by_group('derived' if payload.market=='volatility' else payload.market)
        available={item['symbol'] for item in instruments}
    except (MarketError,TimeoutError,OSError):
        raise HTTPException(503,'Catálogo da Deriv indisponível; configuração não salva') from None
    if not set(payload.symbols).issubset(available):
        raise HTTPException(422,'Seleção contém ativo não disponível no mercado escolhido')
    async with autopilot_lock:
        if user.id in autopilot_enabled or user.id in real_runtime.running:
            raise HTTPException(409,'Desligue o robô antes de alterar sua automação')
        with db.SessionLocal() as session:
            profile=session.get(db.AutomationProfile,user.id)
            if profile is None:
                profile=db.AutomationProfile(user_id=user.id)
                session.add(profile)
            profile.market=payload.market
            profile.account_mode=payload.account_mode
            profile.symbols_json=json.dumps(sorted(payload.symbols))
            profile.strategy=payload.strategy
            profile.expiry_minutes=0
            profile.news_filter=payload.news_filter
            profile.news_minutes_before=payload.news_minutes_before
            profile.news_minutes_after=payload.news_minutes_after
            profile.max_daily_orders=0  # Daily operation-count cap retired; risk gates remain active.
            profile.indicators_json='[]'
            profile.direction=payload.direction
            profile.real_account_id=payload.real_account_id if payload.account_mode=='real' else None
            profile.min_payout=str(payload.min_payout_pct)
            profile.preset_mode=payload.preset_mode
            session.commit()
            result=automation_profile_result(profile)
    return {**result,'saved':True,'running':False,
        'ready_to_start':(payload.account_mode=='demo' and not payload.news_filter),
        'note':'Preferências registradas. A ativação é um comando separado.'}


@app.get('/api/automation/catalogue')
async def automation_catalogue(request:Request,market:str='volatility',
                               nexora_sid:str|None=Cookie(default=None)):
    active_user(request,nexora_sid)
    if market not in (*AUTOMATION_GROUPS,'volatility'):
        raise HTTPException(422,'Mercado inválido')
    try:
        items=await featured_instruments_by_group('derived' if market=='volatility' else market)
    except (MarketError,TimeoutError,OSError):
        raise HTTPException(503,'Não foi possível consultar os ativos Deriv') from None
    return {'market':market,'markets':AUTOMATION_GROUPS,
            'items':[{'symbol':x['symbol'],'name':x['name'],'group':x['group']} for x in items],
            'source':'DERIV_PUBLIC_CATALOG'}


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
        return {**status,**referral,'user_id':'NXR-'+str(user.id).zfill(6),
                'referral_base_url':nexora_referral_origin()}


class BillingCheckoutRequest(BaseModel):
    plan_id: str


@app.get('/api/billing/plans')
async def billing_plans():
    return {'enabled': billing_enabled(),
            'recurring': False,
            'plans': [{'id':key,'name':v['name'],'days':v.get('days',0),
                       'hours':v.get('hours',0),'temporary':key=='teste_1h',
                       'currency':'BRL','amount_cents':v['cents']} for key,v in PLANS.items()
                       if key!='teste_1h' or test_plan_enabled()]}


@app.get('/api/billing/checkout/status')
async def billing_checkout_status(request:Request,session_id:str,
                                  nexora_sid:str|None=Cookie(default=None)):
    """Read-only fulfillment status; a success URL is never payment proof.

    A Stripe-hosted redirect might arrive before its signed webhook. The
    authenticated customer can only inspect their own stored checkout, and
    this endpoint does not retrieve/accept client payment claims or grant time.
    """
    user=active_user(request,nexora_sid)
    if not re.fullmatch(r'cs_[a-zA-Z0-9_-]{4,240}',session_id):
        raise HTTPException(404,'Pagamento não encontrado')
    with db.SessionLocal() as sess:
        order=sess.get(db.StripeAccessPurchase,session_id)
        if order is None or order.user_id!=user.id:
            raise HTTPException(404,'Pagamento não encontrado')
        plan=PLANS.get(order.plan_id)
        if not plan:
            raise HTTPException(503,'Plano indisponível')
        grant=sess.get(db.RobotAccess,user.id)
        return {'plan_id':order.plan_id,'plan_name':plan['name'],
                'amount_cents':order.amount_cents,'currency':'BRL',
                'status':order.status,'confirmed':order.status=='PAID',
                'access_expires_at':(utc(grant.expires_at).isoformat()
                  if order.status=='PAID' and grant else None)}


@app.post('/api/billing/checkout')
async def billing_checkout(request:Request,payload:BillingCheckoutRequest,
                           nexora_sid:str|None=Cookie(default=None)):
    user=active_user(request,nexora_sid)
    if not billing_enabled():
        raise HTTPException(503,'Pagamentos ainda não configurados')
    if not plan_can_purchase(payload.plan_id):
        raise HTTPException(422,'Plano inválido')
    # Checkout returns to the exact verified host that initiated the payment.
    # Both the legacy Render host and the new www host are explicitly allowlisted.
    site_url=checkout_return_origin(request)
    if not site_url or (site_url.startswith('http://') and
                        os.getenv('NEXORA_STRIPE_MODE','').lower()!='test'):
        raise HTTPException(503,'URL pública de cobrança não configurada')
    try:
        checkout=await create_checkout(os.environ['STRIPE_SECRET_KEY'],
                  user_id=user.id,plan_id=payload.plan_id,site_url=site_url)
    except RuntimeError as exc:
        log.warning('Stripe Checkout unavailable for user=%s',user.id)
        raise HTTPException(502,str(exc)) from None
    with db.SessionLocal() as sess:
        sess.add(db.StripeAccessPurchase(
            checkout_session_id=checkout['id'],user_id=user.id,
            plan_id=payload.plan_id,
            amount_cents=PLANS[payload.plan_id]['cents'],
            status='PENDING'))
        sess.commit()
    return {'url':checkout['url']}


@app.post('/api/billing/stripe/webhook')
async def billing_stripe_webhook(request:Request):
    if not billing_enabled():
        raise HTTPException(503,'Webhook de cobrança indisponível')
    raw=await request.body()
    if len(raw)>262144:
        raise HTTPException(413,'Evento muito grande')
    if not stripe_signature_valid(raw,request.headers.get('stripe-signature',''),
                os.environ['STRIPE_WEBHOOK_SECRET']):
        raise HTTPException(400,'Assinatura inválida')
    try:
        event=json.loads(raw)
        kind=event.get('type')
        obj=event['data']['object']
    except (ValueError,KeyError,TypeError,AttributeError):
        raise HTTPException(400,'Evento inválido') from None
    if event.get('livemode') is not stripe_expected_livemode():
        raise HTTPException(400,'Modo de pagamento incompatível com o ambiente')
    if kind not in ('checkout.session.completed','checkout.session.async_payment_succeeded',
                    'checkout.session.async_payment_failed','checkout.session.expired'):
        return {'received':True}
    if not isinstance(obj,dict) or obj.get('livemode') is not stripe_expected_livemode():
        raise HTTPException(400,'Sessão de pagamento inválida')
    if kind in ('checkout.session.async_payment_failed','checkout.session.expired'):
        new_status='FAILED' if kind=='checkout.session.async_payment_failed' else 'EXPIRED'
        sid=obj.get('id')
        if not isinstance(sid,str):
            raise HTTPException(400,'Sessão de pagamento inválida')
        with db.SessionLocal() as sess:
            order=sess.get(db.StripeAccessPurchase,sid,with_for_update=True)
            if order is None:
                # Allow Stripe to retry after checkout creation is committed.
                raise HTTPException(503,'Checkout ainda não registrado')
            if order.status=='PENDING':
                order.status=new_status
                sess.commit()
        return {'received':True,'status':new_status}
    if obj.get('payment_status')!='paid':
        return {'received':True,'status':'awaiting_payment'}
    with db.SessionLocal() as sess:
        result=apply_paid_checkout(sess,obj)
        if result in ('invalid_payment','unknown_plan'):
            log.warning('Stripe payment validation rejected: result=%s',result)
            raise HTTPException(400,'Pagamento inconsistente')
        if result=='unknown_checkout':
            # Stripe retries after transaction creating local checkout settles.
            raise HTTPException(503,'Checkout ainda não registrado')
        sess.commit()
    return {'received':True,'status':result}


from . import admin_auth


class AdminCredentials(BaseModel):
    login:str
    password:str


@app.get('/nexora2')
async def nexora2_workspace(request:Request,nexora_sid:str|None=Cookie(default=None)):
    """Authenticated research workspace; no order execution or mutation."""
    active_user(request,nexora_sid)
    return FileResponse(Path(__file__).parent/'nexora2.html',media_type='text/html',
                        headers={'Cache-Control':'no-store'})


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


@app.get('/api/admin/research/pullback-m1-m3')
async def admin_pullback_research(request:Request):
    """Owner-authenticated read-only EMA20/50 RSI14 pullback backtest."""
    require_nexora_admin(request)
    try:
        report=await asyncio.to_thread(latest_pullback_study)
    except Exception:
        log.exception('Admin pullback study failed')
        raise HTTPException(503, 'Pesquisa pullback indisponível') from None
    if report is None:
        return {'state':'PENDING','symbol':'1HZ100V','message':'Estudo de Pullback ainda não foi concluído.'}
    return report


@app.get('/api/admin/research/multi-timeframe')
async def admin_multi_timeframe_research(request:Request):
    """Authenticated owner-only 96-model research report, no execution controls."""
    require_nexora_admin(request)
    try:
        report=await asyncio.to_thread(latest_multitimeframe_study)
    except Exception:
        log.exception('Admin multitimeframe research read failed')
        raise HTTPException(503,'Pesquisa M1-M15 temporariamente indisponível') from None
    if report is None:
        return {'state':'PENDING','symbol':'1HZ100V',
                'message':'Aguardando histórico M1 completo e validação.'}
    return report


@app.get('/api/admin/research/m1-history')
async def admin_m1_history(request:Request):
    """Owner-only read-only coverage status; never exposes trading controls."""
    require_nexora_admin(request)
    try:
        return await asyncio.to_thread(history_status)
    except Exception:
        log.exception('Admin M1 status read failed')
        raise HTTPException(503, 'Histórico M1 temporariamente indisponível') from None


@app.get('/api/admin/research/volatility100-30d')
async def admin_historical_research(request:Request):
    """Owner-only stored research report; no broker calls or public data exposure."""
    require_nexora_admin(request)
    try:
        report=await asyncio.to_thread(latest_study)
    except Exception:
        log.exception('Admin research read failed')
        raise HTTPException(503,'Relatório temporariamente indisponível') from None
    if report is None:
        return {'state':'PENDING','symbol':'1HZ100V','message':
                'A coleta automática ainda não produziu um relatório válido.',
                'orders_sent':0,'execution_enabled':False}
    return report


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
        if not users:
            return {'items':[]}
        ids=[user.id for user in users]
        # Three bounded queries regardless of the number of users (avoids N+1).
        grants={grant.user_id:grant for grant in sess.scalars(
            select(db.RobotAccess).where(db.RobotAccess.user_id.in_(ids))).all()}
        referral_counts=dict(sess.execute(
            select(db.ReferralCredit.referrer_user_id,
                   func.count(db.ReferralCredit.invitee_user_id))
            .where(db.ReferralCredit.referrer_user_id.in_(ids))
            .group_by(db.ReferralCredit.referrer_user_id)).all())
        now=db.utcnow()
        return {'items':[{
            'user_id':user.id,
            'nexora_id':nexora_user_code(user.id),
            'deriv_account_id':user.deriv_account_id,
            'referrals':referral_counts.get(user.id,0),
            'access':access_status_for_grant(grants.get(user.id),now=now),
        } for user in users]}


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


class PulsoChoiceRequest(BaseModel):
    confirm:str=''


@app.post('/api/autopilot/pulso-v2/select')
async def select_pulso_v2(request:Request,payload:PulsoChoiceRequest,
                           nexora_sid:str|None=Cookie(default=None)):
    """Individual DEMO strategy opt-in; does not start or place an order."""
    user=active_user(request,nexora_sid)
    if payload.confirm!='SELECT_PULSO_V2_DEMO':
        raise HTTPException(403,'Confirmação de estratégia DEMO obrigatória')
    if user.selected_mode!='demo' or not v2_enabled():
        raise HTTPException(403,'PULSO V2 disponível somente no AutoPilot DEMO 2')
    async with autopilot_lock:
        if user.id in autopilot_enabled:
            raise HTTPException(409,'Desligue o robô antes de escolher o PULSO V2')
        with db.SessionLocal() as sess:
            choice=sess.get(db.PilotStrategyChoice,user.id)
            if not choice:
                choice=db.PilotStrategyChoice(user_id=user.id,strategy='pulso_v2')
                sess.add(choice)
            choice.strategy='pulso_v2'
            sess.commit()
    return {'selected':'pulso_v2','running':False,'execution':'DERIV_DEMO_ONLY',
            'message':'PULSO V2 selecionado. Clique LIGAR ROBÔ para iniciar DEMO.'}


async def start_real_autopilot(request:Request,nexora_sid:str|None=None):
    """Explicit opt-in REAL start. No implicit resume after a server restart."""
    user,token=active_session(request,nexora_sid)
    if not real_runtime.real_available():
        raise HTTPException(503,'Operações REAL ainda não foram ativadas neste servidor')
    if user.id in autopilot_enabled:
        raise HTTPException(409,'Desligue o robô DEMO antes de iniciar a conta REAL')
    with db.SessionLocal() as sess:
        profile=sess.get(db.AutomationProfile,user.id)
        plan=sess.get(db.RobotPlan,user.id)
        if not profile or profile.account_mode!='real' or not profile.real_account_id:
            raise HTTPException(409,'Selecione e salve uma conta REAL nas configurações')
        if profile.news_filter:
            raise HTTPException(409,'Filtro de notícias indisponível; desative para usar REAL')
        if plan and (plan.requested_martingale or plan.cycles_limit):
            raise HTTPException(409,'Conta REAL beta exige Martingale e ciclos desativados')
        if user.martingale:
            raise HTTPException(409,'Desative Martingale nas configurações atuais')
        if not access_status(sess,user.id)['allowed']:
            raise HTTPException(403,'Acesso ao robô expirado ou suspenso')
        try:
            stake=Decimal(str(user.stake))
            stop=Decimal(str(user.stop_loss))
            if not stake.is_finite() or not stop.is_finite() or stake<Decimal('0.50') or stop<stake:
                raise ValueError()
        except (InvalidOperation,ValueError):
            raise HTTPException(409,'Configure Stop Loss REAL de pelo menos uma entrada e valor válido') from None
        if profile.strategy not in ('pulso_v2','trend','rsi'):
            raise HTTPException(409,'Estratégia REAL inválida')
        try:
            symbols=json.loads(profile.symbols_json)
        except (ValueError,TypeError):
            raise HTTPException(409,'Seleção de ativos inválida') from None
        if not isinstance(symbols,list) or not 1<=len(symbols)<=5:
            raise HTTPException(409,'Selecione de 1 a 5 ativos para REAL')
        account_id=profile.real_account_id
        pending=sess.scalar(select(db.RealOrder.id).where(
            db.RealOrder.account_id==account_id,
            db.RealOrder.state.in_(('PENDING','UNKNOWN','CONFIRMED'))).limit(1))
        if pending:
            raise HTTPException(409,'Há contrato REAL aberto ou incerto; aguarde conferência')
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res=await client.get('https://api.derivws.com/trading/v1/options/accounts',
                headers={'Authorization':f'Bearer {token}'})
            res.raise_for_status()
            accounts=res.json()
    except (httpx.HTTPError,ValueError):
        raise HTTPException(503,'Não foi possível confirmar a conta REAL com a Deriv') from None
    from .auth_helpers import extract_accounts,real_accounts
    valid={row['account_id'] for row in real_accounts(extract_accounts(accounts))}
    if account_id not in valid:
        raise HTTPException(403,'Conta REAL não pertence à autorização atual')
    try:
        balance=await real_account_balance(token,account_id)
    except RealExecutionError:
        raise HTTPException(503,'Canal de negociação REAL indisponível; nenhuma ordem enviada') from None
    if balance['currency']!='USD':
        raise HTTPException(409,'A versão REAL inicial aceita apenas contas USD')
    if Decimal(balance['balance'])<stake:
        raise HTTPException(409,'Saldo insuficiente na conta REAL')
    async with real_runtime.user_lock(user.id):
        with db.SessionLocal() as sess:
            profile=sess.get(db.AutomationProfile,user.id)
            if (not profile or profile.account_mode!='real'
                or profile.real_account_id!=account_id):
                raise HTTPException(409,'Configuração REAL alterada; inicie novamente')
            consent=sess.get(db.RealRobotConsent,user.id)
            if consent is None:
                consent=db.RealRobotConsent(
                    user_id=user.id,account_id=account_id,
                    max_stake=str(stake),stop_loss=str(stop))
                sess.add(consent)
            else:
                consent.account_id=account_id
                consent.max_stake=str(stake)
                consent.stop_loss=str(stop)
                consent.revoked_at=None
            sess.commit()
        real_runtime.last_scan.pop(user.id,None)
        real_runtime.running.add(user.id)
        real_runtime.reasons[user.id]='Conta REAL ligada; aguardando sinal confirmado'
    return {'running':True,'execution':'DERIV_REAL',
            'real_orders_enabled':True,
            'message':'Compras REAL autorizadas com stop obrigatório e contrato único'}


@app.post('/api/autopilot/start')
async def autopilot_start(request:Request,payload:AutoPilotSwitch,
                          nexora_sid:str|None=Cookie(default=None)):
    if payload.confirm=='START_REAL_AUTOPILOT':
        return await start_real_autopilot(request,nexora_sid)
    user,_=active_session(request,nexora_sid)
    if payload.confirm!='START_DEMO_AUTOPILOT':
        raise HTTPException(403,'Confirme o modo automático de treinamento')
    if not autopilot_allowed():
        raise HTTPException(503,'AutoPilot DEMO não habilitado no servidor')
    if not v2_enabled():
        raise HTTPException(503,'PULSO V2 DEMO indisponível nesta configuração do servidor')
    if user.selected_mode!='demo':
        raise HTTPException(403,'Somente conta demonstrativa')
    with db.SessionLocal() as session:
        profile=session.get(db.AutomationProfile,user.id)
        if profile is None:
            raise HTTPException(409,'Configure e salve os ativos no novo painel antes de ligar o robô')
        if profile is not None:
            if (profile.account_mode!='demo' or profile.market not in (*AUTOMATION_GROUPS,'volatility')
                or profile.strategy not in ('pulso_v2','trend','rsi')
                or profile.expiry_minutes not in (0,1,3,5,15)):
                raise HTTPException(409,'Configuração de automação inválida')
            if profile.news_filter:
                raise HTTPException(409,'Filtro de notícias ainda não conectado: desative para executar DEMO')
            try:
                if not json.loads(profile.symbols_json):
                    raise HTTPException(409,'Escolha pelo menos um ativo')
            except (ValueError,TypeError):
                raise HTTPException(409,'Lista de ativos inválida') from None
    if user.martingale:
        raise HTTPException(409,'Configuração antiga de Martingale: salve novamente as opções atuais')
    with db.SessionLocal() as sess:
        plan=sess.get(db.RobotPlan,user.id)
        if plan and plan.requested_martingale and plan.cycles_limit:
            raise HTTPException(409,'Escolha Martingale OU ciclos antes de ligar o robô; ambos não podem operar juntos')
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
                    # New evaluations run PULSO V2 only, with existing
                    # historical Deriv orders preserved in their ledger.
                    sess.add(db.PilotValidationBatch(
                        user_id=user.id,after_order_id=latest or 0,max_orders=200))
                elif batch.max_orders<200:
                    # Keep existing ledger, risk-session boundaries and results.
                    # This value is metadata, not a cap on PULSO signals.
                    batch.max_orders=200
            if not sess.get(db.AutoPilotResumeConsent,user.id):
                sess.add(db.AutoPilotResumeConsent(user_id=user.id))
            sess.commit()
        autopilot_last_attempt.pop(user.id,None)
        v2_last_scan.pop(user.id,None)
        from .trade_selector_v2 import clear_user_signals
        clear_user_signals(user.id)
        autopilot_enabled.add(user.id)
        log.warning('AutoPilot armed: scan_mode=PULSO_V2; expiry=AUTO_M1_M3_M5_M15; execution=DEMO')
    autopilot_reasons[user.id]='AutoPilot DEMO ativo; buscando sinais com expiração automática conforme perfil salvo'
    return {'running':True,'access':access,'execution':'DERIV_DEMO_ONLY','real_orders_enabled':False}


@app.post('/api/autopilot/stop')
async def autopilot_stop(request:Request,nexora_sid:str|None=Cookie(default=None)):
    user=active_user(request,nexora_sid)
    # Real execution is independently locked and cannot be confused with DEMO.
    if user.id in real_runtime.running:
        async with real_runtime.user_lock(user.id):
            real_runtime.disarm_real(user.id,'Desligado manualmente; contratos REAL continuam em reconciliação')
        return {'running':False,'execution':'DERIV_REAL','real_orders_enabled':real_runtime.real_available()}
    # Wait for any already dispatched broker request to finish before declaring OFF.
    async with autopilot_lock:
        autopilot_enabled.discard(user.id)
        automation_scan_progress.pop(user.id,None)
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
    real_running=user.id in real_runtime.running
    with db.SessionLocal() as sess:
        interrupted=sess.get(db.RobotRun,user.id) is not None and user.id not in autopilot_enabled
        configured_profile=sess.get(db.AutomationProfile,user.id)
        configured_account_mode=configured_profile.account_mode if configured_profile else 'demo'
        batch=sess.get(db.PilotValidationBatch,user.id)
        selected_strategy='pulso_v2' if v2_enabled() else 'unavailable'
        mode='V2_PULSO' if v2_enabled() else 'V1_SINGLE_ASSET'
        upgrade_needed=bool(v2_enabled() and batch is None and user.id in autopilot_enabled)
    async with lock:
        engine=rt_for(user)
        return {'connected':engine.connected,'symbol':engine.symbol,
                'latest_price':engine.prices[-1] if engine.prices else None,
                'ticks':len(engine.prices),'execution':'DERIV_REAL' if real_running else 'DERIV_DEMO_ONLY',
                'running':real_running or user.id in autopilot_enabled,'auto_buy_enabled':autopilot_allowed() or real_runtime.real_available(),
                'real_orders_enabled':real_runtime.real_available(),
                'configured_account_mode':configured_account_mode,
                'interrupted_after_restart':interrupted,
                'autopilot_version':mode,
                'selected_strategy':selected_strategy,
                'v2_available':v2_enabled(),
                'v2_upgrade_needed':upgrade_needed,
                'analysis_symbol':SYMBOL,
                'scan_progress':automation_scan_progress.get(user.id) if user.id in autopilot_enabled else None,
                'reason':real_runtime.reasons.get(user.id,'Conta REAL ativa; buscando oportunidades') if real_running else autopilot_reasons.get(user.id,
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
    return d


@app.get('/api/onboarding/config')
async def get_onboarding_config(request:Request,nexora_sid:str|None=Cookie(default=None)):
    u=active_user(request,nexora_sid)
    with db.SessionLocal() as sess:
        plan=sess.get(db.RobotPlan,u.id)
        cycles=sess.get(db.RobotCycleParams,u.id)
        profile=sess.get(db.AutomationProfile,u.id)
        return {'mode':profile.account_mode if profile else 'demo',
                'real_enabled':real_runtime.real_available(),
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
    if u.id in autopilot_enabled or u.id in real_runtime.running:
        raise HTTPException(409,'Desligue o robô antes de salvar uma nova progressão')
    if payload.gale_count not in range(0,6):
        raise HTTPException(422,'Quantidade de Martingales deve estar entre 0 e 5')
    if payload.cycles not in ('0','1','2','3','4','5'):
        raise HTTPException(422,'Quantidade de ciclos deve estar entre 0 e 5')
    if payload.martingale and payload.gale_count==0:
        raise HTTPException(422,'Para ativar Martingale, escolha entre 1 e 5 tentativas')
    if not payload.martingale and payload.gale_count!=0:
        raise HTTPException(422,'Desative a quantidade de tentativas quando Martingale estiver desligado')
    if payload.martingale and payload.cycles!='0':
        raise HTTPException(422,'Escolha apenas Martingale na próxima vela OU ciclos no próximo sinal, nunca ambos')
    gale_factor=parse_progression_multiplier(payload.multiplier,'Multiplicador Martingale')
    cycle_factor=parse_progression_multiplier(payload.cycle_multiplier,'Multiplicador de ciclos')
    try:
        settings=Settings.parse({'stake':payload.stake,'stop_win':payload.stop_win,
                                 'stop_loss':payload.stop_loss,
                                 'martingale':False,'multiplier':'2'})
    except ValueError as e:
        raise HTTPException(422,str(e)) from None
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
async def single_asset_research_route(request:Request):
    """Owner-only read-only historical study; zero broker buys."""
    require_nexora_admin(request)
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



# Existing shared PostgreSQL research snapshots. This is deliberately NOT the
# current user's query history; no table, migration, order or execution update.
NEXORA2_LIBRARY_LABELS={
    RESEARCH_KEY:'Histórico M5 · 30 dias',
    MULTI_RESEARCH_KEY:'Estudo M1 e M15 · 30 dias',
    PULLBACK_KEY:'Estudo de pullback M1/M3',
}


def nexora2_library_meta(record):
    return {
        'id':record.id,
        'title':NEXORA2_LIBRARY_LABELS[record.research_key],
        'created_at':utc(record.created_at).isoformat(),
        'period_from':utc(record.period_from).isoformat(),
        'period_to':utc(record.period_to).isoformat(),
        'scope':'SHARED_SYSTEM_RESEARCH',
    }


@app.get('/api/nexora2/research/library')
async def nexora2_research_library(request:Request,nexora_sid:str|None=Cookie(default=None)):
    """Authenticated, bounded, read-only persisted studies; never broker orders."""
    active_user(request,nexora_sid)
    with db.SessionLocal() as sess:
        # Metadata projection: do not pull large JSON payloads just to list rows.
        reports=sess.execute(select(
            HistoricalResearchReport.id,HistoricalResearchReport.research_key,
            HistoricalResearchReport.created_at,HistoricalResearchReport.period_from,
            HistoricalResearchReport.period_to
        ).where(
            HistoricalResearchReport.research_key.in_(tuple(NEXORA2_LIBRARY_LABELS))
        ).order_by(HistoricalResearchReport.created_at.desc(),
                   HistoricalResearchReport.id.desc()).limit(12)).all()
        return {
            'items':[nexora2_library_meta(item) for item in reports],
            'source':'EXISTING_POSTGRES_RESEARCH_REPORTS',
            'scope':'SHARED_SYSTEM_RESEARCH','orders_sent':0,
            'note':'Pesquisas globais persistidas, não o histórico individual de consultas. '
                   'A retenção de pesquisas pode remover registros antigos.',
        }


@app.get('/api/nexora2/research/library/{report_id}')
async def nexora2_research_library_detail(request:Request,report_id:int,
                nexora_sid:str|None=Cookie(default=None)):
    """Curated metadata only; do not expose large unreviewed report payloads."""
    active_user(request,nexora_sid)
    with db.SessionLocal() as sess:
        item=sess.scalar(select(HistoricalResearchReport).where(
            HistoricalResearchReport.id==report_id,
            HistoricalResearchReport.research_key.in_(tuple(NEXORA2_LIBRARY_LABELS))
        ))
        if not item:
            raise HTTPException(404,'Pesquisa histórica não encontrada')
        metadata=nexora2_library_meta(item)
        # Reports can include huge research arrays; parse only within a safe bound.
        if len(item.payload_json)>2_000_000:
            raise HTTPException(413,'Relatório grande demais para apresentar neste painel')
        try:
            data=json.loads(item.payload_json)
        except (ValueError,TypeError):
            raise HTTPException(503,'Relatório salvo inválido') from None
        if not isinstance(data,dict):
            raise HTTPException(503,'Formato de relatório não reconhecido')
        results=data.get('results')
        candles=data.get('candles',data.get('m1_candles'))
        coverage=data.get('coverage_pct')
        model_count=len(results) if isinstance(results,list) else None
        # Do not present directional outcomes as financial performance.
        return {**metadata,
            'summary':{
                'candles':candles if isinstance(candles,int) and
                           not isinstance(candles,bool) and 0<=candles<=100_000_000 else None,
                'model_count':model_count if model_count is not None and model_count<=5000 else None,
                'coverage_pct':coverage if isinstance(coverage,(int,float)) and
                     not isinstance(coverage,bool) and isfinite(coverage) and 0<=coverage<=100 else None,
                'source':'PERSISTED_HISTORICAL_RESEARCH',
            },
            'execution_allowed':False,'orders_sent':0,
            'note':'Resumo de pesquisa histórica compartilhada. Não equivale a sinais, '
                   'ordens, lucro realizado ou validação de operações financeiras.',
        }


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
# Existing single scanner + bounded public versioned entries; no new broker
# websocket, process, database, financial executor, or shared user data.
_asset_scanner_research_cache={}
_asset_scanner_last_v2=None


@app.get('/api/research/asset-scanner')
async def asset_scanner_route(request:Request,nexora_sid:str|None=Cookie(default=None),
                              version:int=1,expiry_minutes:int=5):
    """Opt-in I02/I03 public research, with unchanged legacy scanner v1."""
    user=active_user(request,nexora_sid)
    if version not in (1,2,3):
        raise HTTPException(422,'Versão de pesquisa inválida')
    if version==3 and expiry_minutes not in (1,3,5,15):
        raise HTTPException(422,'Expiração de pesquisa inválida')
    if version in (2,3) and os.getenv(
        'NEXORA_INTELLIGENCE_READONLY_ENABLED','false'
    ).strip().lower()!='true':
        raise HTTPException(503,'Intelligence I01-I03 ainda não habilitada')
    global _asset_scanner_cache,_asset_scanner_cache_time,_asset_scanner_last_v2
    async with _asset_scanner_lock:
        now=asyncio.get_running_loop().time()
        if version==1:
            if _asset_scanner_cache is not None and now-_asset_scanner_cache_time<300:
                return {**_asset_scanner_cache,'cached':True}
            try:
                report=await scan_assets()
            except MarketError as exc:
                raise HTTPException(502,str(exc)) from None
            _asset_scanner_cache=report
            _asset_scanner_cache_time=asyncio.get_running_loop().time()
            return {**report,'cached':False}
        key=(version,expiry_minutes if version==3 else 0)
        bucket=int(time.time()//60)
        entry=_asset_scanner_research_cache.get(key)
        cached=bool(entry and now-entry[0]<60
                    and (version!=3 or entry[1]==bucket))
        if cached:
            report=entry[2]
        else:
            try:
                if version==2:
                    report=await scan_intelligence_markets(previous=_asset_scanner_last_v2)
                else:
                    report=await scan_intelligence_context(
                        previous=_asset_scanner_last_v2,expiry_minutes=expiry_minutes)
            except MarketError as exc:
                raise HTTPException(502,str(exc)) from None
            _asset_scanner_research_cache[key]=(
                asyncio.get_running_loop().time(),bucket,report)
            _asset_scanner_last_v2=report
            # At most five version/expiry keys; no tenant data in cache.
            if len(_asset_scanner_research_cache)>5:
                for stale_key in list(_asset_scanner_research_cache):
                    if (stale_key!=key and
                        now-_asset_scanner_research_cache[stale_key][0]>=60):
                        _asset_scanner_research_cache.pop(stale_key,None)
    # Read-only tenant overlay after shared cache; never feeds any trade path.
    from .intelligence2.market_scanner import open_contracts_for_user
    with db.SessionLocal() as sess:
        continuity=open_contracts_for_user(sess,user.id)
    return {**report,'cached':cached,'preserved_open_contracts':continuity,
            'contract_continuity':'READ_ONLY_INDEPENDENT_OF_SELECTION',
            'execution_allowed':False}


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
            # Persist attribution and progression before any Deriv buy request.
            # A failed write must release only its pre-buy reservation, never a
            # contract that may already have been transmitted to the broker.
            try:
                strategy=str(attribution['strategy'])
                if len(strategy)>32:
                    raise ValueError('Identificador de estratégia excede 32 caracteres')
                s.add(db.AutoPilotAttribution(order_id=order_id,user_id=user.id,
                       strategy=strategy,direction=payload.direction,
                       reason=str(attribution['reason'])[:255]))
                if 'cycle_index' in attribution and 'gale_index' in attribution:
                    s.add(db.DemoOrderProgression(order_id=order_id,user_id=user.id,
                        cycle_index=int(attribution['cycle_index']),
                        gale_index=int(attribution['gale_index']),
                        gale_multiplier=str(attribution['gale_multiplier']),
                        cycle_multiplier=str(attribution['cycle_multiplier']),
                        source='autopilot',account_mode='demo'))
                s.commit()
            except Exception:
                s.rollback()
                log.exception('AutoPilot attribution persistence failed before Deriv buy: order=%s',order_id)
                try:
                    with db.SessionLocal() as recovery:
                        pending=recovery.get(db.DemoOrder,order_id,with_for_update=True)
                        if pending and pending.state=='PENDING' and pending.contract_id is None:
                            pending.state='CANCELLED'
                            pending.active_slot=None
                            pending.error_summary='Falha interna antes de enviar compra à Deriv'
                            recovery.commit()
                except Exception:
                    log.exception('Could not release pre-buy reservation: order=%s',order_id)
                raise HTTPException(503,'Falha interna antes do envio à Deriv. Nenhuma compra enviada nesta tentativa.') from None
    # Never hold DB transaction across remote Deriv call.
    try:
        buy_kwargs={'symbol':payload.symbol,'contract_type':payload.direction,
                    'stake':order.stake,'duration':payload.minutes,'confirm':'DEMO_ONLY'}
        if attribution and attribution.get('v2'):
            buy_kwargs['min_payout_ratio']=attribution.get(
                'min_payout_ratio','1.88' if attribution.get('pulso_v2') else '1.50')
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

@app.get('/api/autopilot/pulso-v2/audit')
async def pulso_v2_demo_audit(request:Request,nexora_sid:str|None=Cookie(default=None)):
    """Read-only, account-owned broker settlement audit; no trading changes.

    Do not select favourable symbols based on this report: multiple testing
    and sparse samples require genuinely fresh prospective verification.
    """
    user=active_user(request,nexora_sid)
    from collections import defaultdict
    from decimal import Decimal, InvalidOperation
    from datetime import timedelta

    with db.SessionLocal() as session:
        records=session.execute(
            select(db.DemoOrder,db.AutoPilotAttribution)
            .join(db.AutoPilotAttribution,db.AutoPilotAttribution.order_id==db.DemoOrder.id)
            .where(db.DemoOrder.user_id==user.id,
                   db.AutoPilotAttribution.user_id==user.id,
                   db.AutoPilotAttribution.strategy=='nexora_pulso_v2_stable_call_5m')
            .order_by(db.DemoOrder.id.asc())
        ).all()
    by_symbol=defaultdict(lambda:{
        'settled':0,'wins':0,'losses':0,'draws':0,'pending':0,
        'net_pnl':Decimal('0'),'total_stake':Decimal('0'),
        'first_buy_at':None,'last_buy_at':None
    })
    all_orders=[]
    duplicates=[]
    prior_by_symbol={}
    for o,attribution in records:
        if o.state=='CANCELLED':
            continue
        key=o.symbol
        stat=by_symbol[key]
        buy_at=o.broker_purchase_at or o.created_at
        if buy_at is not None:
            if buy_at.tzinfo is None:
                buy_at=buy_at.replace(tzinfo=timezone.utc)
            iso=buy_at.isoformat()
            stat['first_buy_at']=min(stat['first_buy_at'] or iso,iso)
            stat['last_buy_at']=max(stat['last_buy_at'] or iso,iso)
            previous=prior_by_symbol.get(key)
            if previous is not None:
                prev_at,prev_id,prev_minutes,prev_state=previous
                elapsed=(buy_at-prev_at).total_seconds()
                if 0<=elapsed<prev_minutes*60:
                    duplicates.append({
                        'symbol':key,'earlier_order_id':prev_id,
                        'later_order_id':o.id,
                        'seconds_between_buys':round(elapsed,2),
                        'earlier_state_now':prev_state,
                        'note':'Possível sobreposição temporal; conferir encerramento oficial do primeiro contrato.'
                    })
            prior_by_symbol[key]=(buy_at,o.id,o.duration_minutes,o.state)
        if o.state!='SETTLED' or o.pnl is None:
            stat['pending']+=1
            continue
        try:
            pnl=Decimal(str(o.pnl))
            stake=Decimal(str(o.stake))
        except (InvalidOperation,ValueError,TypeError):
            continue
        if not pnl.is_finite() or not stake.is_finite():
            continue
        stat['settled']+=1
        stat['wins']+=int(pnl>0)
        stat['losses']+=int(pnl<0)
        stat['draws']+=int(pnl==0)
        stat['net_pnl']+=pnl
        stat['total_stake']+=stake
        all_orders.append({
            'id':o.id,'symbol':key,'pnl':float(pnl),
            'stake':float(stake),'direction':o.direction,
            'duration_minutes':o.duration_minutes,
            'buy_at':buy_at.isoformat() if buy_at else None,
        })
    assets=[]
    for symbol,item in sorted(by_symbol.items()):
        n=item['settled']
        assets.append({
            'symbol':symbol,'settled':n,'wins':item['wins'],
            'losses':item['losses'],'draws':item['draws'],
            'pending':item['pending'],
            'win_rate':round(item['wins']/n,4) if n else None,
            'net_pnl':str(item['net_pnl'].quantize(Decimal('0.01'))),
            'total_stake':str(item['total_stake'].quantize(Decimal('0.01'))),
            'first_buy_at':item['first_buy_at'],'last_buy_at':item['last_buy_at']
        })
    total_settled=sum(x['settled'] for x in assets)
    total_wins=sum(x['wins'] for x in assets)
    total_pnl=sum((Decimal(x['net_pnl']) for x in assets),Decimal('0'))
    return {
        'study':'PULSO_V2_OFFICIAL_DEMO_AUDIT',
        'scope':'AUTHENTICATED_USER_ALL_PULSO_V2_HISTORY',
        'execution':'READ_ONLY',
        'assets':assets,
        'overall':{
            'settled':total_settled,'wins':total_wins,
            'losses':sum(x['losses'] for x in assets),
            'draws':sum(x['draws'] for x in assets),
            'pending':sum(x['pending'] for x in assets),
            'win_rate':round(total_wins/total_settled,4) if total_settled else None,
            'net_pnl':str(total_pnl.quantize(Decimal('0.01')))
        },
        'potential_overlaps':duplicates,
        'orders_counted':len(all_orders),
        'caveat':'Sobreposição por horário de compra não comprova duplicidade; comparar liquidação oficial. Retornos anteriores não demonstram vantagem futura.'
    }


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

async def _recover_account_orders(user, token, mode, account_id):
    """Refresh one authenticated Deriv account without sending any trade request."""
    if mode=='demo':
        model=db.DemoOrder
        active_states=('PENDING','CONFIRMED','UNKNOWN')
        status_call=demo_contract_status
        from .demo import APP_ID as recovery_app_id
        ledger_link=link_recovered_contract
        ledger_absent=mark_broker_absent
    else:
        from .real_ledger import (RealOrderGuardError, link_recovered_real_contract,
                                  mark_real_broker_absent, settle_real_order)
        model=db.RealOrder
        active_states=('PENDING','CONFIRMED','UNKNOWN')
        status_call=real_contract_status
        from .real_broker import APP_ID as recovery_app_id
        ledger_link=link_recovered_real_contract
        ledger_absent=mark_real_broker_absent

    with db.SessionLocal() as session:
        query=select(model).where(model.user_id==user.id,
                                  model.state.in_(active_states))
        if mode=='real':
            query=query.where(model.account_id==account_id)
        rows=session.scalars(query.order_by(model.id.asc())).all()
        if not rows:
            return {'mode':mode,'checked':0,'linked':0,'settled':0,
                    'released':0,'blocked':0,'message':'Nenhuma operação pendente nesta conta.'}
        order_data=[{'id':row.id,'contract_id':row.contract_id,
                     'created_at':row.created_at,'updated_at':row.updated_at,
                     'state':row.state} for row in rows]
        known_ids={str(row.contract_id) for row in session.scalars(
            select(model).where(model.contract_id.is_not(None))).all()}

    oldest=min(row['created_at'] for row in order_data)
    if oldest.tzinfo is None:
        oldest=oldest.replace(tzinfo=timezone.utc)
    date_from=max(946684800,int(oldest.timestamp())-3600)
    date_to=int(time.time())
    try:
        snapshot=await account_recovery_snapshot(mode,token,account_id,date_from,date_to)
    except RecoverySnapshotError as exc:
        raise HTTPException(502,str(exc)) from None
    if snapshot.get('complete') is not True:
        raise HTTPException(502,'A Deriv não confirmou a consulta completa; bloqueios preservados.')

    decisions={}
    contract_claims={}
    for item in order_data:
        if item['contract_id']:
            continue
        with db.SessionLocal() as session:
            order=session.get(model,item['id'])
            if not order:
                continue
            classification,cid,evidence=classify_unidentified_order(
                order,snapshot['contracts'],snapshot['transactions'],recovery_app_id)
        if classification=='MATCH' and (cid in known_ids):
            classification,cid,evidence='AMBIGUOUS',None,'contrato já associado a outro registro'
        decisions[item['id']]=(classification,cid,evidence)
        if classification=='MATCH':
            contract_claims.setdefault(cid,[]).append(item['id'])
    for cid,order_ids in contract_claims.items():
        if len(order_ids)>1:
            for order_id in order_ids:
                decisions[order_id]=('AMBIGUOUS',None,'mesmo contrato compatível com mais de uma reserva')

    counts={'checked':len(order_data),'linked':0,'settled':0,'released':0,'blocked':0}
    status_targets=[]
    async with reconcile_lock if mode=='demo' else real_runtime.user_lock(user.id):
        with db.SessionLocal() as session:
            for item in order_data:
                order=session.scalar(select(model).where(
                    model.id==item['id']).with_for_update())
                if not order or order.state not in active_states:
                    continue
                cid=str(order.contract_id) if order.contract_id else None
                if cid:
                    status_targets.append((order.id,cid))
                    continue
                classification,candidate_id,evidence=decisions.get(
                    order.id,('AMBIGUOUS',None,'reserva alterada durante a consulta'))
                if classification=='MATCH':
                    try:
                        ledger_link(session,order,evidence)
                        counts['linked']+=1
                        status_targets.append((order.id,str(candidate_id)))
                    except Exception as exc:
                        session.rollback()
                        log.warning('Recovery link rejected: mode=%s order=%s error=%s',
                                    mode,order.id,type(exc).__name__)
                        counts['blocked']+=1
                elif classification=='ABSENT':
                    # PENDING may still be inside a broker buy request. For
                    # UNKNOWN, wait five full minutes after the state changed
                    # to UNKNOWN, not merely after reservation creation; the
                    # just-failed request must age out before a snapshot can
                    # release its durable slot.
                    if order.state!='UNKNOWN':
                        counts['blocked']+=1
                        continue
                    uncertain_since=order.updated_at or order.created_at
                    if uncertain_since.tzinfo is None:
                        uncertain_since=uncertain_since.replace(tzinfo=timezone.utc)
                    if time.time()-uncertain_since.timestamp()<300:
                        counts['blocked']+=1
                        continue
                    try:
                        if ledger_absent(session,order,verified=True):
                            counts['released']+=1
                    except Exception as exc:
                        session.rollback()
                        log.warning('Recovery release rejected: mode=%s order=%s error=%s',
                                    mode,order.id,type(exc).__name__)
                        counts['blocked']+=1
                else:
                    counts['blocked']+=1

    # Known or newly linked contract IDs are checked through Deriv's official
    # contract-status request. Errors never release a slot.
    settle_call=settle if mode=='demo' else settle_real_order
    for order_id,cid in status_targets:
        try:
            receipt=await status_call(token,account_id,cid)
            contract=receipt.get('contract')
            if not isinstance(contract,dict):
                counts['blocked']+=1
                continue
        except Exception as exc:
            if isinstance(exc,asyncio.CancelledError):
                raise
            counts['blocked']+=1
            log.warning('Recovery status unavailable: mode=%s order=%s error=%s',
                        mode,order_id,type(exc).__name__)
            continue
        if mode=='demo':
            async with reconcile_lock:
                with db.SessionLocal() as session:
                    order=session.scalar(select(model).where(model.id==order_id,
                        model.user_id==user.id,model.contract_id==cid))
                    if order and order.state in active_states:
                        try:
                            if settle_call(session,order,contract):
                                counts['settled']+=1
                        except OrderGuardError:
                            session.rollback();counts['blocked']+=1
        else:
            async with real_runtime.user_lock(user.id):
                with db.SessionLocal() as session:
                    order=session.scalar(select(model).where(model.id==order_id,
                        model.user_id==user.id,model.account_id==account_id,
                        model.contract_id==cid))
                    if order and order.state in active_states:
                        try:
                            if settle_call(session,order,contract):
                                counts['settled']+=1
                        except RealOrderGuardError:
                            session.rollback();counts['blocked']+=1

    if counts['released']:
        log.warning('Deriv absence confirmed; durable reservation marked cancelled: mode=%s count=%s',
                    mode,counts['released'])
    counts['mode']=mode
    counts['message']='Sincronização concluída; confira o status atualizado.'
    return counts

@app.post('/api/orders/recover')
async def recover_orders_route(request:Request,nexora_sid:str|None=Cookie(default=None)):
    """Reconnect read-only to Deriv and reconcile only the signed-in user's active orders."""
    user,token=active_session(request,nexora_sid)
    with db.SessionLocal() as session:
        profile=session.get(db.AutomationProfile,user.id)
        mode=profile.account_mode if profile and profile.account_mode in ('demo','real') else 'demo'
        real_account_id=profile.real_account_id if mode=='real' else None
        consent=session.get(db.RealRobotConsent,user.id) if mode=='real' else None
        if mode=='real' and (not real_account_id or not consent
                             or consent.account_id!=real_account_id):
            raise HTTPException(403,'Conta REAL sem autorização vinculada; nenhuma operação foi alterada.')
    if mode=='demo':
        return await _recover_account_orders(user,token,'demo',user.deriv_account_id)

    # The REAL path is read-only, but still revalidates account ownership from
    # the authenticated Deriv session before requesting the account OTP.
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response=await client.get('https://api.derivws.com/trading/v1/options/accounts',
                headers={'Authorization':f'Bearer {token}'})
            response.raise_for_status()
            payload=response.json()
        from .auth_helpers import extract_accounts,real_accounts
        valid={row['account_id'] for row in real_accounts(extract_accounts(payload))}
        if real_account_id not in valid:
            raise HTTPException(403,'A Deriv não confirmou a conta REAL selecionada; bloqueios preservados.')
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(502,'Não foi possível revalidar a conta REAL na Deriv; bloqueios preservados.') from None
    return await _recover_account_orders(user,token,'real',real_account_id)

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
