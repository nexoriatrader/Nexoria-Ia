"""NEXORA IA v0.4. Public-host foundation, strictly virtual tick-based simulation.
No broker BUY/SELL endpoint exists, no real/demo contract orders.
"""
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
from .engine import Engine, Settings
from .market import MarketError, discover_symbols, discover_contracts, indicative_proposal
from .security import CLIENT_ID, REDIRECT, TOKEN_URL, build_authorize_url, make_login, make_session_id, hashsecret, encrypt, decrypt, cookie_options

log=logging.getLogger('nexora')
PUBLIC_WS='wss://api.derivws.com/trading/v1/options/ws/public'
SYMBOL=os.getenv('NEXORA_SYMBOL','1HZ100V')
# Per-user runtime states are separate and intentionally temporary.
runtimes: dict[int, Engine] = {}
market_task = None
market_connected = False
recent_prices=[]
last_market_reason='Aguardando conexão'
lock=asyncio.Lock()

def rt_for(user):
    engine=runtimes.get(user.id)
    if not engine:
        engine=Engine()
        engine.symbol=SYMBOL
        engine.settings=Settings.parse({'stake':user.stake,'martingale':user.martingale,'multiplier':user.multiplier,
            'stop_win':user.stop_win,'stop_loss':user.stop_loss})
        engine.connected=market_connected
        engine.prices.extend(recent_prices[-200:])
        engine.source='Deriv público (simulação local)'
        runtimes[user.id]=engine
    return engine

def active_user(request:Request, sid):
    if not sid: raise HTTPException(401,'Faça login com Deriv')
    with db.SessionLocal() as s:
        obj=s.get(db.Session,hashsecret(sid))
        expiry=obj.expires_at.replace(tzinfo=timezone.utc) if obj and obj.expires_at.tzinfo is None else (obj.expires_at if obj else None)
        if not obj or expiry<=db.utcnow(): raise HTTPException(401,'Sessão expirada; conecte novamente')
        user=s.get(db.User,obj.user_id)
        if not user: raise HTTPException(401,'Usuário não encontrado')
        return user

async def feed():
    global market_connected, last_market_reason, recent_prices
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
                        engine.connected=True;engine.prices.clear();engine.running=False
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
                        async with lock:
                            recent_prices=(recent_prices+[p])[-200:]
                            for uid,engine in list(runtimes.items()):
                                before=len(engine.closed)
                                engine.push_tick(p)
                                if len(engine.closed)>before:
                                    event=engine.closed[0]
                                    # Persist completed paper trades independently of the browser.
                                    with db.SessionLocal() as s:
                                        s.add(db.PaperTrade(user_id=uid,symbol=event['symbol'],direction=event['direction'],
                                            stake=event['stake'], entry=str(event['entry']),exit=str(event['exit']),
                                            pnl=event['pnl'],result=event['result']))
                                        s.commit()
        except asyncio.CancelledError: raise
        except Exception as e:
            log.warning('Public feed unavailable: %s',type(e).__name__)
            async with lock:
                market_connected=False;last_market_reason='Conexão interrompida'
                for engine in runtimes.values():
                    engine.running=False;engine.connected=False
                    # Active virtual positions are cancelled on interruption, not settled with guessed prices.
                    engine.position=None
                    engine.last_reason='Conexão interrompida: simulação pausada; operação virtual aberta cancelada'
            await asyncio.sleep(delay)
            delay=min(delay*2,30)

@asynccontextmanager
async def lifespan(app):
    global market_task
    if os.getenv('NEXORA_PUBLIC_URL','').startswith('https://') and db.DB_URL.startswith('sqlite:'):
        raise RuntimeError('Não usar SQLite efêmero em implantação pública: configure DATABASE_URL de Postgres')
    db.init_db()
    market_task=asyncio.create_task(feed())
    yield
    market_task.cancel()
    try:await market_task
    except asyncio.CancelledError:pass

app=FastAPI(title='NEXORA IA',version='0.4',lifespan=lifespan)

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

@app.get('/healthz')
async def health(): return {'ok':True,'version':'0.4','market_connected':market_connected,'execution':'PAPER_ONLY'}

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
                async with lock:
                    engine=runtimes.get(session.user_id)
                    if engine:engine.running=False
                s.delete(session);s.commit()
    response=JSONResponse({'ok':True});response.delete_cookie('nexora_sid',path='/');return response

class Config(BaseModel):
    stake:str='2.00';martingale:bool=False;multiplier:str='2.00';stop_win:str='20.00';stop_loss:str='10.00'

@app.get('/api/status')
async def status(request:Request,nexora_sid:str|None=Cookie(default=None)):
    u=active_user(request,nexora_sid)
    async with lock:
        engine=rt_for(u)
        return {**engine.status(),'execution':'LOCAL_SIMULATION_ONLY','real_orders_enabled':False}

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

@app.post('/api/start')
async def start(request:Request,nexora_sid:str|None=Cookie(default=None)):
    u=active_user(request,nexora_sid)
    async with lock:
        engine=rt_for(u)
        if not market_connected:raise HTTPException(409,'Mercado desconectado')
        if engine.position:raise HTTPException(409,'Existe operação virtual aberta')
        reason=engine.guard_reason(engine.next_stake())
        if reason:raise HTTPException(409,reason)
        engine.running=True;engine.last_reason='Simulação virtual ativada; NÃO opera na Deriv'
    return {'ok':True}

@app.post('/api/stop')
async def stop(request:Request,nexora_sid:str|None=Cookie(default=None)):
    u=active_user(request,nexora_sid)
    async with lock:
        engine=rt_for(u);engine.running=False;engine.last_reason='Simulação desligada'
    return {'ok':True}

@app.get('/api/history')
async def history(request:Request,nexora_sid:str|None=Cookie(default=None)):
    u=active_user(request,nexora_sid)
    with db.SessionLocal() as s:
        items=s.execute(select(db.PaperTrade).where(db.PaperTrade.user_id==u.id).order_by(db.PaperTrade.id.desc()).limit(50)).scalars().all()
        return {'items':[{'time':x.settled_at.isoformat(),'symbol':x.symbol,'direction':x.direction,
                          'stake':x.stake,'entry':x.entry,'exit':x.exit,'pnl':x.pnl,'result':x.result} for x in items]}

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
