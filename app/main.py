"""NEXORA IA. Official Deriv DEMO orders and read-only AutoPilot research."""
import asyncio, json, logging, os, secrets, re
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import websockets
from fastapi import FastAPI, HTTPException, Cookie, Request
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import select, delete

from . import db
from .demo import DemoTradingError, demo_account_balance, demo_contract_status, demo_buy, demo_proposal
from .demo_ledger import OrderGuardError, reserve, public_order, confirmed, unknown, settle
from .autopilot import analyze, quote_assessment
from .engine import Engine, Settings
from .market import MarketError, discover_symbols, discover_contracts, indicative_proposal
from .strategy_lab import run_lab
from .security import CLIENT_ID, REDIRECT, TOKEN_URL, build_authorize_url, make_login, make_session_id, hashsecret, encrypt, decrypt, cookie_options

log=logging.getLogger('nexora')
VERSION='0.8.0'
PUBLIC_WS='wss://api.derivws.com/trading/v1/options/ws/public'
SYMBOL=os.getenv('NEXORA_SYMBOL','1HZ100V')
# Per-user runtime states are separate and intentionally temporary.
runtimes: dict[int, Engine] = {}
market_task = None
reconcile_task = None
autopilot_task = None
# User opt-in is intentionally volatile: service restarts always turn bots OFF.
autopilot_enabled: set[int] = set()
autopilot_last_attempt: dict[int,float] = {}
autopilot_reasons: dict[int,str] = {}
autopilot_lock = asyncio.Lock()
market_last_tick = 0.0
market_connected = False
recent_prices=[]
last_market_reason='Aguardando conexão'
lock=asyncio.Lock()
reconcile_lock=asyncio.Lock()

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
            log.warning('Public feed unavailable: %s',type(e).__name__)
            async with lock:
                market_connected=False;last_market_reason='Conexão interrompida'
                for engine in runtimes.values():
                    engine.connected=False
                    engine.last_reason='Conexão de cotações interrompida'
            await asyncio.sleep(delay)
            delay=min(delay*2,30)


def autopilot_allowed():
    return (os.getenv('NEXORA_AUTOPILOT_DEMO_ENABLED','false').lower()=='true'
            and os.getenv('NEXORA_DEMO_BUY_ENABLED','false').lower()=='true')


async def autopilot_once():
    """Continuously discover experimental candidates; broker enforces one active order."""
    if not autopilot_enabled:
        return
    now=asyncio.get_running_loop().time()
    async with lock:
        connected=market_connected and market_last_tick>0 and now-market_last_tick<35
        prices=list(recent_prices)
    if not connected:
        for uid in tuple(autopilot_enabled):
            autopilot_reasons[uid]='Aguardando cotações recentes da Deriv'
        return
    research=analyze(prices)
    if not research['candidates']:
        for uid in tuple(autopilot_enabled):
            autopilot_reasons[uid]='Buscando oportunidade: '+research['reason']
        return
    candidates=research['candidates']
    for uid in tuple(autopilot_enabled):
        if uid not in autopilot_enabled:
            continue
        # Throttle checks; order_guard independently prevents overlapping orders.
        if now-autopilot_last_attempt.get(uid,-1e12)<60:
            continue
        async with autopilot_lock:
            if uid not in autopilot_enabled or not autopilot_allowed():
                continue
            with db.SessionLocal() as sess:
                user=sess.get(db.User,uid)
                if not user or user.selected_mode!='demo':
                    autopilot_enabled.discard(uid)
                    autopilot_reasons[uid]='Conta demonstrativa indisponível; robô desligado'
                    continue
                if user.martingale:
                    autopilot_enabled.discard(uid)
                    autopilot_reasons[uid]='Desative Martingale nas configurações para usar o AutoPilot'
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
                    autopilot_enabled.discard(uid)
                    autopilot_reasons[uid]='Sessão Deriv expirada; conecte novamente'
                    continue
                orders=sess.scalars(select(db.DemoOrder).where(db.DemoOrder.user_id==uid)).all()
                # Rotate the available independent hypotheses, recording each source.
                previous=sess.scalars(select(db.AutoPilotAttribution.id)
                        .where(db.AutoPilotAttribution.user_id==uid)).all()
                candidate=candidates[len(previous)%len(candidates)]
                from .demo_ledger import order_guard
                try:
                    order_guard(user,orders,user.stake)
                except OrderGuardError as e:
                    message=str(e)
                    if 'Stop' in message or 'Martingale' in message:
                        autopilot_enabled.discard(uid)
                    autopilot_reasons[uid]=message
                    continue
                # Detached user snapshot survives session scope (expire_on_commit=False).
                account=user.deriv_account_id
            # Repeat authorization and feed checks before broker call.
            if uid not in autopilot_enabled:
                continue
            autopilot_last_attempt[uid]=now
            key='auto-'+secrets.token_hex(16)
            autopilot_reasons[uid]='Enviando contrato oficial DEMO: '+candidate['strategy']
            try:
                result=await perform_demo_buy(user,token,
                     DemoBuy(symbol=SYMBOL,direction=candidate['direction'],minutes=5,
                             confirm='DEMO_ONLY',request_key=key),
                     attribution={'strategy':candidate['strategy'],'reason':candidate['reason']})
                autopilot_reasons[uid]='Contrato DEMO #'+str(result['id'])+' confirmado; aguardando liquidação'
            except HTTPException as e:
                # An unknown or pending buy remains blocked in the durable order ledger.
                message=str(e.detail)
                autopilot_reasons[uid]=message
                if e.status_code in (401,403,502,503) or 'Stop' in message:
                    autopilot_enabled.discard(uid)
            except Exception as e:
                autopilot_enabled.discard(uid)
                autopilot_reasons[uid]='Falha na execução; robô desligado para segurança'
                log.warning('AutoPilot execution halted: user=%s error=%s',uid,type(e).__name__)


async def autopilot_loop():
    while True:
        try:
            await autopilot_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning('AutoPilot loop error: %s',type(exc).__name__)
        await asyncio.sleep(10)


async def reconcile_confirmed_orders_once():
    """Refresh only already-confirmed demo contracts; this task never sends buys."""
    with db.SessionLocal() as s:
        order_ids=s.scalars(select(db.DemoOrder.id).where(db.DemoOrder.state=='CONFIRMED')
                            .order_by(db.DemoOrder.id).limit(100)).all()
    for order_id in order_ids:
        async with reconcile_lock:
            with db.SessionLocal() as s:
                order=s.get(db.DemoOrder,order_id)
                if not order or order.state!='CONFIRMED' or not order.contract_id:
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
                    if not order or order.state!='CONFIRMED':
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
        await asyncio.sleep(15)

@asynccontextmanager
async def lifespan(app):
    global market_task, reconcile_task, autopilot_task
    if os.getenv('NEXORA_PUBLIC_URL','').startswith('https://') and db.DB_URL.startswith('sqlite:'):
        raise RuntimeError('Não usar SQLite efêmero em implantação pública: configure DATABASE_URL de Postgres')
    db.init_db()
    market_task=asyncio.create_task(feed())
    reconcile_task=asyncio.create_task(reconcile_confirmed_loop())
    autopilot_task=asyncio.create_task(autopilot_loop())
    yield
    autopilot_enabled.clear()
    tasks=[task for task in (market_task,reconcile_task,autopilot_task) if task]
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
async def login_start():
    state,verifier=make_login()
    with db.SessionLocal() as s:
        # expiry handled at consumption; old flows can be pruned in maintenance
        s.add(db.LoginFlow(state_hash=hashsecret(state),verifier_encrypted=encrypt(verifier)));s.commit()
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
        if user is None:
            user=db.User(deriv_account_id=account);s.add(user);s.flush()
        autopilot_enabled.discard(user.id)
        autopilot_reasons[user.id]='Nova sessão Deriv: ative o AutoPilot manualmente'
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
            'real_enabled':False}

@app.post('/api/logout')
async def logout(request:Request,nexora_sid:str|None=Cookie(default=None)):
    if nexora_sid:
        with db.SessionLocal() as s:
            session=s.get(db.Session,hashsecret(nexora_sid))
            if session:
                autopilot_enabled.discard(session.user_id)
                autopilot_reasons[session.user_id]='Sessão encerrada; AutoPilot desligado'
                async with lock:
                    engine=runtimes.get(session.user_id)
                    if engine:engine.running=False
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
        raise HTTPException(409,'Desative Martingale para ativar o AutoPilot')
    with db.SessionLocal() as sess:
        orders=sess.scalars(select(db.DemoOrder).where(db.DemoOrder.user_id==user.id)).all()
        from .demo_ledger import order_guard
        try:
            order_guard(user,orders,user.stake)
        except OrderGuardError as e:
            if 'Contrato demo em andamento' not in str(e):
                raise HTTPException(409,str(e)) from None
            # Permit arming during a previously confirmed contract; no new order until settled.
            if any(o.state in ('PENDING','UNKNOWN') for o in orders):
                raise HTTPException(409,'Contrato pendente ou incerto: reconciliação necessária')
    autopilot_enabled.add(user.id)
    autopilot_reasons[user.id]='AutoPilot ligado; aguardando oportunidade oficial na Deriv Demo'
    return {'running':True,'execution':'DERIV_DEMO_ONLY','real_orders_enabled':False}


@app.post('/api/autopilot/stop')
async def autopilot_stop(request:Request,nexora_sid:str|None=Cookie(default=None)):
    user=active_user(request,nexora_sid)
    # Wait for any already dispatched broker request to finish before declaring OFF.
    async with autopilot_lock:
        autopilot_enabled.discard(user.id)
        autopilot_reasons[user.id]='Desligado: novas entradas bloqueadas; contratos abertos permanecem na Deriv'
    return {'running':False,'execution':'DERIV_DEMO_ONLY','real_orders_enabled':False}


@app.get('/api/status')
async def status(request:Request,nexora_sid:str|None=Cookie(default=None)):
    user=active_user(request,nexora_sid)
    async with lock:
        engine=rt_for(user)
        return {'connected':engine.connected,'symbol':engine.symbol,
                'latest_price':engine.prices[-1] if engine.prices else None,
                'ticks':len(engine.prices),'execution':'DERIV_DEMO_ONLY',
                'running':user.id in autopilot_enabled,'auto_buy_enabled':autopilot_allowed(),
                'real_orders_enabled':False,
                'reason':autopilot_reasons.get(user.id,'Robô desligado')}

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
    u=active_user(request,nexora_sid)
    try:settings=Settings.parse(config.model_dump())
    except ValueError as e:raise HTTPException(400,str(e)) from None
    async with lock:
        engine=rt_for(u)
        try:engine.configure(settings)
        except ValueError as e:raise HTTPException(409,str(e)) from None
        with db.SessionLocal() as s:
            stored=s.get(db.User,u.id)
            stored.stake=str(settings.stake);stored.stop_win=str(settings.stop_win);stored.stop_loss=str(settings.stop_loss)
            stored.multiplier=str(settings.multiplier);stored.martingale=settings.martingale
            s.commit()
    return {'ok':True}

# Account onboarding config uses the same safe demo risk settings as /api/config.
# Martingale and cycle controls are PLANNED preferences only: execution stays single-stake.
class OnboardingConfig(BaseModel):
    stake:str='2.00'
    stop_win:str='0.00'
    stop_loss:str='0.00'
    martingale:bool=False
    gale_count:int=0
    multiplier:str='2.00'
    cycles:str='unlimited'


@app.get('/api/onboarding/config')
async def get_onboarding_config(request:Request,nexora_sid:str|None=Cookie(default=None)):
    u=active_user(request,nexora_sid)
    with db.SessionLocal() as sess:
        plan=sess.get(db.RobotPlan,u.id)
        return {'mode':'demo','real_enabled':False,
                'stake':u.stake,'stop_win':u.stop_win,'stop_loss':u.stop_loss,
                'martingale':bool(plan.requested_martingale) if plan else False,
                'gale_count':plan.gale_count if plan else 0,
                'multiplier':plan.requested_multiplier if plan else '2.00',
                'cycles':str(plan.cycles_limit) if plan and plan.cycles_limit is not None else 'unlimited',
                'martingale_execution_enabled':False,'cycles_execution_enabled':False}


@app.post('/api/onboarding/config')
async def save_onboarding_config(request:Request,payload:OnboardingConfig,
                                 nexora_sid:str|None=Cookie(default=None)):
    u=active_user(request,nexora_sid)
    if not isinstance(payload.martingale,bool):
        raise HTTPException(422,'Valor de Martingale inválido')
    if payload.gale_count not in (0,1,2,3):
        raise HTTPException(422,'Quantidade de martingales inválida')
    if payload.cycles not in ('1','3','5','10','unlimited'):
        raise HTTPException(422,'Quantidade de ciclos inválida')
    if payload.martingale and payload.gale_count==0:
        raise HTTPException(422,'Escolha pelo menos uma tentativa de Martingale')
    if not payload.martingale and payload.gale_count!=0:
        raise HTTPException(422,'Desative as tentativas de Martingale')
    # For current official orders the only enabled progression is flat stake.
    try:
        settings=Settings.parse({'stake':payload.stake,'stop_win':payload.stop_win,
                                 'stop_loss':payload.stop_loss,
                                 'martingale':False,'multiplier':'2'})
        selected_multiplier=Settings.parse({'stake':payload.stake,
                                            'stop_win':payload.stop_win,'stop_loss':payload.stop_loss,
                                            'martingale':False,'multiplier':payload.multiplier}).multiplier
    except ValueError as e:
        raise HTTPException(422,str(e)) from None
    async with lock:
        engine=rt_for(u)
        try:engine.configure(settings)
        except ValueError as e:raise HTTPException(409,str(e)) from None
        with db.SessionLocal() as sess:
            stored=sess.get(db.User,u.id)
            stored.stake=str(settings.stake)
            stored.stop_win=str(settings.stop_win)
            stored.stop_loss=str(settings.stop_loss)
            stored.martingale=False
            stored.multiplier='2.00'
            plan=sess.get(db.RobotPlan,u.id)
            if not plan:
                plan=db.RobotPlan(user_id=u.id)
                sess.add(plan)
            plan.requested_martingale=payload.martingale
            plan.gale_count=payload.gale_count
            plan.requested_multiplier=str(selected_multiplier)
            plan.cycles_limit=None if payload.cycles=='unlimited' else int(payload.cycles)
            sess.commit()
    return {'ok':True,'execution_mode':'DEMO_FLAT_STAKE',
            'martingale_execution_enabled':False,'cycles_execution_enabled':False,
            'message':'Limites salvos. Martingale e ciclos guardados como preferências futuras, ainda sem execução.'}


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

async def perform_demo_buy(user, token, payload:DemoBuy, *, attribution:dict|None=None):
    """Single guarded broker purchase path for both manual and AutoPilot DEMO orders."""
    if payload.confirm != 'DEMO_ONLY':
        raise HTTPException(403,'Confirmação de conta demo obrigatória')
    if os.getenv('NEXORA_DEMO_BUY_ENABLED','false').lower() != 'true':
        raise HTTPException(503,'Compra demonstrativa desabilitada')
    if user.selected_mode!='demo':
        raise HTTPException(403,'Somente conta demonstrativa')
    if not re.fullmatch(r'[A-Za-z0-9]{2,30}',payload.symbol):
        raise HTTPException(422,'Ativo inválido')
    with db.SessionLocal() as s:
        stored=s.get(db.User,user.id)
        try:
            order,created=reserve(s,stored,request_key=payload.request_key,
                                  symbol=payload.symbol,direction=payload.direction,
                                  minutes=payload.minutes)
        except OrderGuardError as e:
            raise HTTPException(409,str(e)) from None
        order_id=order.id
        if not created:
            return {**public_order(order),'replayed':True,'no_new_buy':True}
        if attribution:
            # Commit strategy before network I/O. If attribution fails, do NOT buy.
            s.add(db.AutoPilotAttribution(order_id=order_id,user_id=user.id,
                   strategy=attribution['strategy'],direction=payload.direction,
                   reason=str(attribution['reason'])[:255]))
            s.commit()
    # Never hold DB transaction across remote Deriv call.
    try:
        receipt=await demo_buy(token, user.deriv_account_id, symbol=payload.symbol,
                 contract_type=payload.direction, stake=order.stake,
                 duration=payload.minutes, confirm='DEMO_ONLY')
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
        records=session.execute(
            select(db.AutoPilotAttribution, db.DemoOrder)
            .join(db.DemoOrder,db.AutoPilotAttribution.order_id==db.DemoOrder.id)
            .where(db.AutoPilotAttribution.user_id==user.id)
        ).all()
    summary={}
    for attribution,order in records:
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
            'note':'Resultados observados de contratos oficiais; não validam predição futura'}


@app.get('/api/demo/orders')
async def demo_orders_route(request:Request,nexora_sid:str|None=Cookie(default=None)):
    user=active_user(request,nexora_sid)
    with db.SessionLocal() as s:
        orders=s.scalars(select(db.DemoOrder).where(db.DemoOrder.user_id==user.id)
                       .order_by(db.DemoOrder.id.desc()).limit(100)).all()
        return {'items':[public_order(o) for o in orders], 'execution':'DERIV_DEMO_ONLY'}

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
