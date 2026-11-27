import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app import db, main, real_runtime
from app.security import encrypt, hashsecret

@pytest.fixture
def api(tmp_path,monkeypatch):
    engine=create_engine(f"sqlite:///{tmp_path}/manual.sqlite")
    db.Base.metadata.create_all(engine);factory=sessionmaker(bind=engine,expire_on_commit=False)
    monkeypatch.setattr(db,'SessionLocal',factory);monkeypatch.setattr(main.db,'SessionLocal',factory)
    client=TestClient(main.app)
    yield client,factory
    engine.dispose()

def add_manual_user(factory,*,account_mode='demo'):
    sid='manual-session-cookie'
    with factory() as s:
        user=db.User(deriv_account_id='V9001',selected_mode='demo',stake='2.00',stop_win='20',stop_loss='10')
        s.add(user);s.flush()
        s.add(db.Session(session_hash=hashsecret(sid),user_id=user.id,token_encrypted=encrypt('mock-token'),expires_at=db.utcnow()+timedelta(hours=2)))
        s.add(db.AutomationProfile(user_id=user.id,operation_mode='manual',account_mode=account_mode,
            market='volatility',symbols_json='[]',strategy='manual',real_account_id='R9001' if account_mode=='real' else None))
        db.ensure_panel_profiles(s,user.id)
        s.commit();uid=user.id
    return sid,uid

def test_manual_demo_cycle_uses_official_legacy_loss_for_next_scheduled_signal(api,monkeypatch):
    _,factory=api;_,uid=add_manual_user(factory)
    now=db.utcnow()
    with factory() as s:
        run=db.ManualSignalRun(user_id=uid,session_number=1,status='RUNNING',account_mode='demo')
        plan=db.RobotPlan(user_id=uid,requested_martingale=False,gale_count=0,
            requested_multiplier='2.00',cycles_limit=2,updated_at=now-timedelta(minutes=20))
        cycle_cfg=db.RobotCycleParams(user_id=uid,multiplier='2.50')
        panel=s.get(db.AutomationPanelProfile,(uid,'manual'))
        panel.cycles_limit=2;panel.cycle_multiplier='2.50';panel.stake='2.00'
        panel.updated_at=now-timedelta(minutes=20)
        s.get(db.AutomationExecutionState,uid).active_panel_mode='manual'
        loss=db.DemoOrder(user_id=uid,request_key='manual-loss-before-cycle-fix',
            state='SETTLED',symbol='frxEURUSD',direction='CALL',duration_minutes=15,
            stake='2.00',pnl='-2.00',created_at=now-timedelta(minutes=10))
        s.add_all([run,plan,cycle_cfg,loss]);s.flush()
        s.add_all([
            db.ManualSignal(user_id=uid,session_number=1,capacity_slot=None,line_number=1,
                raw_line='M15 EURUSD 19:00 CALL',market='forex',timezone_name='UTC',
                local_date=now.date().isoformat(),local_time='19:00',
                scheduled_at=now-timedelta(minutes=15),symbol='frxEURUSD',direction='CALL',
                duration_minutes=15,status='COMPLETED',request_key=loss.request_key,
                ledger_mode='demo',ledger_order_id=loss.id,created_at=now-timedelta(minutes=11)),
            db.ManualSignal(user_id=uid,session_number=1,capacity_slot=1,line_number=2,
                raw_line='M15 EURUSD 19:15 PUT',market='forex',timezone_name='UTC',
                local_date=now.date().isoformat(),local_time='19:15',
                scheduled_at=now-timedelta(seconds=5),symbol='frxEURUSD',direction='PUT',
                duration_minutes=15,status='SCHEDULED',request_key='manual-next-after-loss',
                created_at=now-timedelta(minutes=11)),
        ])
        s.commit()

    monkeypatch.setattr(main,'autopilot_allowed',lambda:True)
    buy=AsyncMock(return_value={'id':987654})
    monkeypatch.setattr(main,'perform_demo_buy',buy)
    main.autopilot_enabled.add(uid)
    try:
        asyncio.run(main._run_manual_demo_signal(uid))
        assert buy.await_count==1
        args=buy.await_args
        assert args.args[2].direction=='PUT'
        assert str(args.kwargs['stake_override'])=='5.00'
        assert args.kwargs['attribution']['source']=='manual'
        assert args.kwargs['attribution']['cycle_index']==1
        with factory() as s:
            signal=s.query(db.ManualSignal).filter_by(request_key='manual-next-after-loss').one()
            assert signal.status=='EXECUTED' and signal.ledger_mode=='demo'
    finally:
        main.autopilot_enabled.discard(uid)

def test_manual_demo_martingale_uses_profile_stake_after_official_loss(api,monkeypatch):
    _,factory=api;_,uid=add_manual_user(factory)
    now=db.utcnow()
    with factory() as s:
        run=db.ManualSignalRun(user_id=uid,session_number=1,status='RUNNING',account_mode='demo')
        profile=s.get(db.AutomationPanelProfile,(uid,'manual'))
        profile.stake='2.00';profile.requested_martingale=True;profile.gale_count=2
        profile.requested_multiplier='2.50';profile.cycles_limit=0
        profile.updated_at=now-timedelta(minutes=20)
        s.get(db.AutomationExecutionState,uid).active_panel_mode='manual'
        loss=db.DemoOrder(user_id=uid,request_key='manual-martingale-official-loss',
            state='SETTLED',symbol='frxEURUSD',direction='CALL',duration_minutes=15,
            stake='2.00',pnl='-2.00',created_at=now-timedelta(minutes=10))
        s.add_all([run,loss]);s.flush()
        s.add_all([
            db.ManualSignal(user_id=uid,session_number=1,capacity_slot=None,line_number=1,
                raw_line='M15 EURUSD 19:00 CALL',market='forex',timezone_name='UTC',
                local_date=now.date().isoformat(),local_time='19:00',
                scheduled_at=now-timedelta(minutes=15),symbol='frxEURUSD',direction='CALL',
                duration_minutes=15,status='COMPLETED',request_key=loss.request_key,
                ledger_mode='demo',ledger_order_id=loss.id,created_at=now-timedelta(minutes=11)),
            db.ManualSignal(user_id=uid,session_number=1,capacity_slot=1,line_number=2,
                raw_line='M15 EURUSD 19:15 PUT',market='forex',timezone_name='UTC',
                local_date=now.date().isoformat(),local_time='19:15',
                scheduled_at=now-timedelta(seconds=5),symbol='frxEURUSD',direction='PUT',
                duration_minutes=15,status='SCHEDULED',request_key='manual-after-martingale-loss',
                created_at=now-timedelta(minutes=11)),
        ])
        s.commit()

    monkeypatch.setattr(main,'autopilot_allowed',lambda:True)
    buy=AsyncMock(return_value={'id':987655})
    monkeypatch.setattr(main,'perform_demo_buy',buy)
    main.autopilot_enabled.add(uid)
    try:
        asyncio.run(main._run_manual_demo_signal(uid))
        assert buy.await_count==1
        assert buy.await_args.args[2].direction=='PUT'
        assert str(buy.await_args.kwargs['stake_override'])=='5.00'
        assert buy.await_args.kwargs['attribution']['gale_index']==1
        assert buy.await_args.kwargs['attribution']['gale_multiplier']=='2.50'
        assert buy.await_args.kwargs['attribution']['cycle_index']==0
    finally:
        main.autopilot_enabled.discard(uid)

def test_manual_demo_cycle_waits_for_unknown_official_order(api,monkeypatch):
    _,factory=api;_,uid=add_manual_user(factory)
    now=db.utcnow()
    with factory() as s:
        s.add(db.ManualSignalRun(user_id=uid,session_number=1,status='RUNNING',account_mode='demo'))
        s.get(db.AutomationExecutionState,uid).active_panel_mode='manual'
        order=db.DemoOrder(user_id=uid,request_key='manual-uncertain-order',state='UNKNOWN',
            symbol='frxEURUSD',direction='CALL',duration_minutes=15,stake='2.00')
        s.add(order);s.flush()
        s.add_all([
            db.ManualSignal(user_id=uid,session_number=1,capacity_slot=1,line_number=1,
                raw_line='M15 EURUSD 19:00 CALL',market='forex',timezone_name='UTC',
                local_date=now.date().isoformat(),local_time='19:00',scheduled_at=now,
                symbol='frxEURUSD',direction='CALL',duration_minutes=15,status='UNKNOWN',
                request_key=order.request_key,ledger_mode='demo',ledger_order_id=order.id),
            db.ManualSignal(user_id=uid,session_number=1,capacity_slot=2,line_number=2,
                raw_line='M15 EURUSD 19:15 PUT',market='forex',timezone_name='UTC',
                local_date=now.date().isoformat(),local_time='19:15',
                scheduled_at=now-timedelta(seconds=5),symbol='frxEURUSD',direction='PUT',
                duration_minutes=15,status='SCHEDULED',request_key='manual-after-unknown'),
        ])
        s.commit()
    monkeypatch.setattr(main,'autopilot_allowed',lambda:True)
    buy=AsyncMock(side_effect=AssertionError('uncertain official order must block another entry'))
    monkeypatch.setattr(main,'perform_demo_buy',buy)
    main.autopilot_enabled.add(uid)
    main.autopilot_reasons.pop(uid,None)
    try:
        asyncio.run(main._run_manual_demo_signal(uid))
        assert not buy.called
        with factory() as s:
            assert s.get(db.DemoOrder,order.id).state=='UNKNOWN'
            assert s.query(db.ManualSignal).filter_by(status='SCHEDULED').one().status=='SCHEDULED'
    finally:
        main.autopilot_enabled.discard(uid)

def test_manual_real_cycles_are_rejected_before_any_broker_query(api,monkeypatch):
    client,factory=api;sid,uid=add_manual_user(factory,account_mode='real')
    client.cookies.set('nexora_sid',sid)
    with factory() as s:
        s.get(db.AutomationPanelProfile,(uid,'manual')).cycles_limit=1
        s.commit()
    monkeypatch.setattr(real_runtime,'real_available',lambda:True)
    response=client.post('/api/autopilot/start',json={'confirm':'START_REAL_AUTOPILOT','panel_mode':'manual'})
    assert response.status_code==409
    assert 'ciclos desativados' in response.json()['detail']

def test_manual_profile_needs_no_strategy_assets_and_preview_is_read_only(api,monkeypatch):
    client,factory=api;sid,uid=add_manual_user(factory);client.cookies.set('nexora_sid',sid)
    catalog=[{'symbol':'frxEURUSD','name':'EUR/USD','group':'forex'},
             {'symbol':'R_100','name':'Volatility 100','group':'derived'}]
    monkeypatch.setattr(main,'market_instruments',AsyncMock(return_value=catalog))
    buy=AsyncMock(side_effect=AssertionError('preview must never buy'))
    monkeypatch.setattr(main,'demo_buy',buy)
    result=client.post('/api/manual-signals/preview',json={'text':'M15 EURUSD 23:00 CALL\nM5 R_100 23:01 PUT',
        'market':'forex','day':'tomorrow','timezone_name':'UTC'})
    assert result.status_code==200
    body=result.json()
    assert len(body['valid'])==1 and body['valid'][0]['symbol']=='frxEURUSD'
    assert any(i['code']=='outside_market' for i in body['issues'])
    assert not buy.called

def test_manual_expired_access_blocks_preview_and_confirmation_but_keeps_status_and_cancel(api,monkeypatch):
    client,factory=api;sid,uid=add_manual_user(factory);client.cookies.set('nexora_sid',sid)
    monkeypatch.setenv('NEXORA_ACCESS_CONTROL_ENABLED','true')
    expired_at=db.utcnow()-timedelta(seconds=5)
    with factory() as s:
        s.add(db.RobotAccess(user_id=uid,access_kind='trial_24h',
            starts_at=expired_at-timedelta(hours=24),expires_at=expired_at))
        s.add(db.ManualSignalRun(user_id=uid,session_number=1,status='RUNNING',account_mode='demo'))
        s.get(db.AutomationExecutionState,uid).active_panel_mode='manual'
        s.commit()
    market=AsyncMock(side_effect=AssertionError('expired access must block signal validation'))
    monkeypatch.setattr(main,'market_instruments',market)

    preview=client.post('/api/manual-signals/preview',json={'text':'M15 EURUSD 23:00 CALL',
        'market':'forex','day':'tomorrow','timezone_name':'UTC'})
    confirmed=client.post('/api/manual-signals/confirm',json={'text':'M15 EURUSD 23:00 CALL',
        'market':'forex','day':'tomorrow','timezone_name':'UTC','idempotency_key':'expired-access-01'})
    assert preview.status_code==403 and 'tempo de acesso' in preview.json()['detail']
    assert confirmed.status_code==403 and 'tempo de acesso' in confirmed.json()['detail']
    assert not market.called

    status=client.get('/api/manual-signals/status')
    cancelled=client.post('/api/manual-signals/cancel')
    assert status.status_code==200 and status.json()['persisted_status']=='RUNNING'
    assert cancelled.status_code==200 and not cancelled.json()['cancel_pending']

def test_manual_preview_starts_trial_for_legacy_account_without_separate_timer(api,monkeypatch):
    client,factory=api;sid,uid=add_manual_user(factory);client.cookies.set('nexora_sid',sid)
    monkeypatch.setenv('NEXORA_ACCESS_CONTROL_ENABLED','true')
    monkeypatch.setattr(main,'market_instruments',AsyncMock(return_value=[
        {'symbol':'frxEURUSD','name':'EUR/USD','group':'forex'}]))
    preview=client.post('/api/manual-signals/preview',json={'text':'M15 EURUSD 23:00 CALL',
        'market':'forex','day':'tomorrow','timezone_name':'UTC'})
    assert preview.status_code==200
    with factory() as s:
        grant=s.get(db.RobotAccess,uid)
        assert grant is not None and grant.access_kind=='trial_24h'
        first_expiry=grant.expires_at
    repeated=client.post('/api/manual-signals/preview',json={'text':'M15 EURUSD 23:15 PUT',
        'market':'forex','day':'tomorrow','timezone_name':'UTC'})
    assert repeated.status_code==200
    with factory() as s:
        assert s.get(db.RobotAccess,uid).expires_at==first_expiry

def test_manual_confirmation_is_idempotent_limited_to_active_session_and_status(api,monkeypatch):
    client,factory=api;sid,uid=add_manual_user(factory);client.cookies.set('nexora_sid',sid)
    monkeypatch.setenv('NEXORA_AUTOPILOT_DEMO_ENABLED','true');monkeypatch.setenv('NEXORA_DEMO_BUY_ENABLED','true')
    monkeypatch.setattr(main,'market_instruments',AsyncMock(return_value=[{'symbol':'frxEURUSD','name':'EUR/USD','group':'forex'}]))
    monkeypatch.setattr(main,'perform_demo_buy',AsyncMock(side_effect=AssertionError('schedule test cannot buy')))
    started=client.post('/api/autopilot/start',json={'confirm':'START_DEMO_AUTOPILOT','panel_mode':'manual'})
    assert started.status_code==200 and started.json()['operation_mode']=='manual'
    data={'text':'M15 EURUSD 23:00 CALL','market':'forex','day':'tomorrow','timezone_name':'UTC','idempotency_key':'manual-test-001'}
    first=client.post('/api/manual-signals/confirm',json=data)
    second=client.post('/api/manual-signals/confirm',json=data)
    assert first.status_code==200 and second.status_code==200
    assert first.json()['confirmed'][0]['id']==second.json()['confirmed'][0]['id']
    assert first.json()['pending_count']==1 and second.json()['pending_count']==1
    changed_payload={**data,'text':'M15 EURUSD 23:05 CALL'}
    reused_key=client.post('/api/manual-signals/confirm',json=changed_payload)
    assert reused_key.status_code==409 and 'outra lista' in reused_key.json()['detail']
    assert not main.perform_demo_buy.called
    status=client.get('/api/manual-signals/status').json()
    assert status['running'] and status['pending_count']==1
    signal=status['signals'][0]
    assert signal['market']=='forex' and signal['timezone_name']=='UTC'
    assert signal['local_date'] and signal['local_time']=='23:00'
    with factory() as s:
        assert s.query(db.ManualSignalBatch).count()==1
    stopped=client.post('/api/autopilot/stop',json={'panel_mode':'manual'})
    assert stopped.status_code==200 and stopped.json()['persisted_status']=='PAUSED'
    retry_after_pause=client.post('/api/manual-signals/confirm',json=data)
    assert retry_after_pause.status_code==200
    assert retry_after_pause.json()==first.json()
    status=client.get('/api/status?panel_mode=manual&fresh=true').json()
    assert status['operation_mode']=='manual' and status['manual_session']['status']=='PAUSED'
    assert main.autopilot_enabled.isdisjoint({uid})

def test_manual_preview_and_status_require_authenticated_session(api):
    client,_=api
    assert client.post('/api/manual-signals/preview',json={'text':'','market':'forex','day':'today'}).status_code==401
    assert client.post('/api/manual-signals/confirm',json={'text':'','market':'forex','day':'today','idempotency_key':'manual-key-01'}).status_code==401
    assert client.get('/api/manual-signals/status').status_code==401

def test_completed_manual_list_can_be_turned_off_before_switching_to_strategies(api,monkeypatch):
    client,factory=api;sid,uid=add_manual_user(factory);client.cookies.set('nexora_sid',sid)
    now=datetime.now(timezone.utc)
    with factory() as s:
        run=db.ManualSignalRun(user_id=uid,session_number=1,status='COMPLETED',account_mode='demo',completed_at=now)
        signal=db.ManualSignal(user_id=uid,session_number=1,capacity_slot=None,line_number=1,
            raw_line='M15 EURUSD 19:00 CALL',market='forex',timezone_name='UTC',
            local_date=now.date().isoformat(),local_time='19:00',scheduled_at=now,
            symbol='frxEURUSD',direction='CALL',duration_minutes=15,status='EXECUTED',
            request_key='completed-manual-test',ledger_mode='demo')
        s.add_all([run,signal]);s.commit()
    # Simulate the stale in-memory state that can still block profile saves after the queue ends.
    main.autopilot_enabled.add(uid)
    try:
        state=client.get('/api/manual-signals/status').json()
        assert not state['running'] and state['persisted_status']=='COMPLETED'
        assert state['pending_count']==0 and state['signals']==[]
        stopped=client.post('/api/autopilot/stop',json={'panel_mode':'manual'})
        assert stopped.status_code==200 and stopped.json()['persisted_status']=='PAUSED'
        assert 'Robô Manual desligado' in stopped.json()['message']
        assert uid not in main.autopilot_enabled
        with factory() as s:
            assert s.get(db.ManualSignalRun,uid).status=='PAUSED'
            assert s.query(db.ManualSignal).one().status=='EXECUTED'
        monkeypatch.setattr(main,'strategy_allowed',lambda *args:True)
        monkeypatch.setattr(main,'featured_instruments_by_group',AsyncMock(return_value=[
            {'symbol':'frxEURUSD','name':'EUR/USD','group':'forex'}]))
        changed=client.post('/api/automation/profile',json={'panel_mode':'strategies','operation_mode':'strategies',
            'account_mode':'demo','market':'forex','symbols':['frxEURUSD'],
            'strategy':'trend','expiry_minutes':15})
        assert changed.status_code==200 and changed.json()['operation_mode']=='strategies'
    finally:
        main.autopilot_enabled.discard(uid)

def test_real_manual_executor_does_not_call_broker_for_no_due_signals(api,monkeypatch):
    _,factory=api;_,uid=add_manual_user(factory,account_mode='real')
    with factory() as s:
        user=s.query(db.User).one();profile=s.get(db.AutomationPanelProfile,(uid,'manual'))
        run=db.ManualSignalRun(user_id=uid,session_number=1,status='RUNNING',account_mode='real',real_account_id='R9001')
        consent=db.RealRobotConsent(user_id=uid,account_id='R9001',max_stake='2',stop_loss='10')
        s.add_all([run,consent]);s.commit();s.expunge(user);s.expunge(profile);s.expunge(consent)
    buy=AsyncMock(side_effect=AssertionError('no due signal means no broker purchase'))
    monkeypatch.setattr(real_runtime,'buy_real_contract',buy)
    real_runtime.running.add(uid)
    try:
        asyncio.run(real_runtime._manual_real_signal(uid,user,profile,consent,'mock-token'))
        assert not buy.called
    finally:real_runtime.running.discard(uid)

def test_manual_profile_save_skips_strategy_and_asset_requirements(api,monkeypatch):
    client,factory=api;sid,uid=add_manual_user(factory);client.cookies.set('nexora_sid',sid)
    catalogue=AsyncMock(side_effect=AssertionError('manual profile save must not require a selected asset catalogue'))
    monkeypatch.setattr(main,'featured_instruments_by_group',catalogue)
    response=client.post('/api/automation/profile',json={'panel_mode':'manual','operation_mode':'manual','account_mode':'demo',
        'market':'volatility','symbols':[],'strategy':'','expiry_minutes':0})
    assert response.status_code==200
    profile=response.json()
    assert profile['operation_mode']=='manual' and profile['strategy']=='manual' and profile['symbols']==[]
    assert not catalogue.called

@pytest.mark.parametrize('target_mode,target_account,initial_account',[
    ('manual','demo','real'),       # Account change while retaining Manual.
    ('strategies','demo','demo'),   # Leaving Manual while its pending queue exists.
])
def test_profile_cannot_change_execution_identity_while_manual_signals_are_pending(api,monkeypatch,target_mode,target_account,initial_account):
    client,factory=api;sid,uid=add_manual_user(factory,account_mode=initial_account);client.cookies.set('nexora_sid',sid)
    now=datetime.now(timezone.utc)
    with factory() as s:
        run=db.ManualSignalRun(user_id=uid,session_number=1,status='PAUSED',account_mode=initial_account,
            real_account_id='R9001' if initial_account=='real' else None)
        signal=db.ManualSignal(user_id=uid,session_number=1,capacity_slot=1,line_number=1,
            raw_line='M15 EURUSD 23:00 CALL',market='forex',timezone_name='UTC',
            local_date=(now+timedelta(hours=1)).date().isoformat(),local_time='23:00',
            scheduled_at=now+timedelta(hours=1),symbol='frxEURUSD',direction='CALL',
            duration_minutes=15,status='SCHEDULED',request_key='pending-profile-guard')
        s.add_all([run,signal]);s.commit()
    monkeypatch.setattr(main,'strategy_allowed',lambda *args:True)
    monkeypatch.setattr(main,'featured_instruments_by_group',AsyncMock(return_value=[
        {'symbol':'frxEURUSD','name':'EUR/USD','group':'forex'}]))
    payload={'panel_mode':target_mode,'operation_mode':target_mode,'account_mode':target_account,'market':'forex',
        'symbols':['frxEURUSD'] if target_mode=='strategies' else [],
        'strategy':'pulso_v2' if target_mode=='strategies' else '',
        'expiry_minutes':15 if target_mode=='strategies' else 0}
    response=client.post('/api/automation/profile',json=payload)
    assert response.status_code==409 and 'sinais pendentes' in response.json()['detail']
    with factory() as s:
        profile=s.get(db.AutomationPanelProfile,(uid,'manual'))
        assert profile.operation_mode=='manual' and profile.account_mode==initial_account
        assert s.query(db.ManualSignal).one().status=='SCHEDULED'

def test_manual_capacity_is_100_pending_per_session(api,monkeypatch):
    client,factory=api;sid,uid=add_manual_user(factory);client.cookies.set('nexora_sid',sid)
    monkeypatch.setenv('NEXORA_AUTOPILOT_DEMO_ENABLED','true');monkeypatch.setenv('NEXORA_DEMO_BUY_ENABLED','true')
    monkeypatch.setattr(main,'market_instruments',AsyncMock(return_value=[{'symbol':'frxEURUSD','name':'EUR/USD','group':'forex'}]))
    monkeypatch.setattr(main,'perform_demo_buy',AsyncMock(side_effect=AssertionError('future signals are only queued')))
    assert client.post('/api/autopilot/start',json={'confirm':'START_DEMO_AUTOPILOT','panel_mode':'manual'}).status_code==200
    lines='\n'.join(f'M1 EURUSD {hour:02d}:{minute:02d} CALL' for hour,minute in
        [(i//60,i%60) for i in range(101)])
    response=client.post('/api/manual-signals/confirm',json={'text':lines,'market':'forex',
        'day':'tomorrow','timezone_name':'UTC','idempotency_key':'cap-test-0001'})
    assert response.status_code==200
    body=response.json()
    assert len(body['confirmed'])==100 and body['pending_count']==100 and body['remaining_capacity']==0
    assert any(issue['code']=='capacity' for issue in body['issues'])
    assert not main.perform_demo_buy.called
    main.autopilot_enabled.discard(uid)

def test_due_demo_signal_uses_official_buy_adapter_with_exact_duration_and_deadline(api,monkeypatch):
    _,factory=api;_,uid=add_manual_user(factory)
    with factory() as s:
        s.get(db.AutomationPanelProfile,(uid,'manual')).stake='0.50'
        s.commit()
    monkeypatch.setattr(main,'autopilot_allowed',lambda:True)
    main.autopilot_enabled.add(uid)
    due=datetime.now(timezone.utc)-timedelta(seconds=5)
    with factory() as s:
        s.get(db.AutomationExecutionState,uid).active_panel_mode='manual'
        s.add(db.ManualSignalRun(user_id=uid,session_number=1,status='RUNNING',account_mode='demo'))
        s.add(db.ManualSignal(user_id=uid,session_number=1,capacity_slot=1,line_number=1,
            raw_line='M15 EURUSD 19:00 CALL',market='forex',timezone_name='America/Sao_Paulo',
            local_date=due.date().isoformat(),local_time=due.strftime('%H:%M'),scheduled_at=due,
            symbol='frxEURUSD',direction='CALL',duration_minutes=15,status='SCHEDULED',
            request_key='manual-demo-due-test'))
        s.commit()
    buy=AsyncMock(return_value={'id':9876})
    monkeypatch.setattr(main,'perform_demo_buy',buy)
    try:
        asyncio.run(main._run_manual_demo_signal(uid))
        assert buy.await_count==1
        _,token,payload=buy.call_args.args
        assert token=='mock-token'
        assert payload.symbol=='frxEURUSD' and payload.direction=='CALL' and payload.minutes==15
        assert buy.call_args.kwargs['latest_buy_send_epoch']==pytest.approx(due.timestamp()+60-0.001)
        assert buy.call_args.kwargs['stake_override']=='0.50'
        with factory() as s:
            row=s.query(db.ManualSignal).one()
            assert row.status=='EXECUTED' and row.ledger_mode=='demo' and row.capacity_slot is None
    finally:
        main.autopilot_enabled.discard(uid)

def test_real_manual_dispatch_never_calls_strategy_scanner(api,monkeypatch):
    _,factory=api;_,uid=add_manual_user(factory,account_mode='real')
    with factory() as s:
        s.get(db.AutomationExecutionState,uid).active_panel_mode='manual'
        s.add(db.RealRobotConsent(user_id=uid,account_id='R9001',max_stake='2.00',stop_loss='10.00',panel_mode='manual'))
        s.commit()
    manual_worker=AsyncMock(return_value=None)
    scanner=AsyncMock(side_effect=AssertionError('Manual REAL must not scan strategies'))
    monkeypatch.setattr(real_runtime,'real_available',lambda:True)
    monkeypatch.setattr(real_runtime,'_manual_real_signal',manual_worker)
    monkeypatch.setattr(real_runtime,'confirmed_automation_signal',scanner)
    last_scan_before=real_runtime.last_scan.get(uid)
    real_runtime.running.add(uid)
    try:
        asyncio.run(real_runtime._user_scan(uid))
        assert manual_worker.await_count==1
        assert manual_worker.call_args.args[0]==uid
        assert not scanner.called
        assert real_runtime.last_scan.get(uid)==last_scan_before
    finally:
        real_runtime.running.discard(uid)


@pytest.mark.parametrize('account_mode', ['demo','real'])
def test_manual_exhausted_list_turns_robot_off_and_hides_signal_without_touching_ledger(api,account_mode):
    client,factory=api;sid,uid=add_manual_user(factory,account_mode=account_mode)
    client.cookies.set('nexora_sid',sid)
    now=datetime.now(timezone.utc)
    with factory() as s:
        run=db.ManualSignalRun(user_id=uid,session_number=1,status='RUNNING',
            account_mode=account_mode,real_account_id='R9001' if account_mode=='real' else None)
        if account_mode=='demo':
            order=db.DemoOrder(user_id=uid,request_key='manual-final-demo-order',active_slot=uid,
                state='CONFIRMED',contract_id='99001',symbol='frxEURUSD',direction='CALL',
                duration_minutes=15,stake='2.00')
        else:
            order=db.RealOrder(user_id=uid,account_id='R9001',request_key='manual-final-real-order',
                active_slot='R9001',state='CONFIRMED',contract_id='99002',symbol='frxEURUSD',
                direction='CALL',duration_minutes=15,stake='2.00',currency='USD')
            s.add(db.RealRobotConsent(user_id=uid,account_id='R9001',max_stake='2.00',stop_loss='10.00',panel_mode='manual'))
        s.add_all([run,order]);s.flush()
        s.add(db.ManualSignal(user_id=uid,session_number=1,capacity_slot=None,line_number=1,
            raw_line='M15 EURUSD 19:00 CALL',market='forex',timezone_name='UTC',
            local_date=now.date().isoformat(),local_time='19:00',scheduled_at=now,
            symbol='frxEURUSD',direction='CALL',duration_minutes=15,status='EXECUTED',
            request_key='manual-final-signal-'+account_mode,ledger_mode=account_mode,
            ledger_order_id=order.id))
        s.commit()
    if account_mode=='demo':main.autopilot_enabled.add(uid)
    else:real_runtime.running.add(uid)
    try:
        asyncio.run(main.reconcile_manual_lifecycle_once())
        with factory() as s:
            run=s.get(db.ManualSignalRun,uid)
            order=(s.query(db.DemoOrder).one() if account_mode=='demo' else s.query(db.RealOrder).one())
            assert run.status=='COMPLETED'
            assert 'automaticamente' in run.pause_reason
            assert order.state=='CONFIRMED'  # accepted contract remains untouched for Deriv reconciliation
        state=client.get('/api/manual-signals/status').json()
        assert state['persisted_status']=='COMPLETED' and state['list_completed']
        assert state['signals']==[] and state['pending_count']==0
        assert uid not in (main.autopilot_enabled if account_mode=='demo' else real_runtime.running)
    finally:
        main.autopilot_enabled.discard(uid);real_runtime.running.discard(uid)


@pytest.mark.parametrize('account_mode', ['demo','real'])
def test_manual_cancel_waits_for_official_order_settlement_then_cancels_future_rows(api,account_mode):
    client,factory=api;sid,uid=add_manual_user(factory,account_mode=account_mode)
    client.cookies.set('nexora_sid',sid)
    now=datetime.now(timezone.utc)
    with factory() as s:
        run=db.ManualSignalRun(user_id=uid,session_number=1,status='RUNNING',
            account_mode=account_mode,real_account_id='R9001' if account_mode=='real' else None)
        if account_mode=='demo':
            order=db.DemoOrder(user_id=uid,request_key='manual-current-demo-order',active_slot=uid,
                state='CONFIRMED',contract_id='99101',symbol='frxEURUSD',direction='CALL',
                duration_minutes=15,stake='2.00')
        else:
            order=db.RealOrder(user_id=uid,account_id='R9001',request_key='manual-current-real-order',
                active_slot='R9001',state='CONFIRMED',contract_id='99102',symbol='frxEURUSD',
                direction='CALL',duration_minutes=15,stake='2.00',currency='USD')
            s.add(db.RealRobotConsent(user_id=uid,account_id='R9001',max_stake='2.00',stop_loss='10.00',panel_mode='manual'))
        s.add_all([run,order]);s.flush()
        s.add_all([
            db.ManualSignal(user_id=uid,session_number=1,capacity_slot=None,line_number=1,
                raw_line='M15 EURUSD 19:00 CALL',market='forex',timezone_name='UTC',
                local_date=now.date().isoformat(),local_time='19:00',scheduled_at=now,
                symbol='frxEURUSD',direction='CALL',duration_minutes=15,status='EXECUTED',
                request_key='manual-current-signal-'+account_mode,ledger_mode=account_mode,
                ledger_order_id=order.id),
            db.ManualSignal(user_id=uid,session_number=1,capacity_slot=1,line_number=2,
                raw_line='M15 EURUSD 19:15 PUT',market='forex',timezone_name='UTC',
                local_date=now.date().isoformat(),local_time='19:15',scheduled_at=now+timedelta(minutes=15),
                symbol='frxEURUSD',direction='PUT',duration_minutes=15,status='SCHEDULED',
                request_key='manual-future-signal-'+account_mode),
        ])
        s.commit()
    if account_mode=='demo':main.autopilot_enabled.add(uid)
    else:real_runtime.running.add(uid)
    try:
        response=client.post('/api/manual-signals/cancel')
        assert response.status_code==200 and response.json()['cancel_pending']
        with factory() as s:
            run=s.get(db.ManualSignalRun,uid)
            current=s.query(db.ManualSignal).filter_by(status='EXECUTED').one()
            future=s.query(db.ManualSignal).filter_by(status='SCHEDULED').one()
            order=(s.query(db.DemoOrder).one() if account_mode=='demo' else s.query(db.RealOrder).one())
            assert run.status=='CANCELLING'
            assert future.status=='SCHEDULED' and current.status=='EXECUTED'
            assert order.state=='CONFIRMED'
        # A worker restart must preserve the blocking cancellation state; an
        # accepted but unsettled official-ledger contract is not cancellable.
        main.pause_manual_sessions_after_restart()
        asyncio.run(main.reconcile_manual_lifecycle_once())
        with factory() as s:
            assert s.get(db.ManualSignalRun,uid).status=='CANCELLING'
            assert s.query(db.ManualSignal).filter_by(status='SCHEDULED').count()==1
        # The queue action never cancels a purchased contract. Simulate the
        # official settlement receipt; lifecycle reconciliation cancels only future rows.
        with factory() as s:
            order=(s.query(db.DemoOrder).one() if account_mode=='demo' else s.query(db.RealOrder).one())
            order.state='SETTLED';order.pnl='0.25';s.commit()
        asyncio.run(main.reconcile_manual_lifecycle_once())
        with factory() as s:
            run=s.get(db.ManualSignalRun,uid)
            statuses={row.status for row in s.query(db.ManualSignal).all()}
            order=(s.query(db.DemoOrder).one() if account_mode=='demo' else s.query(db.RealOrder).one())
            assert run.status=='COMPLETED'
            assert statuses=={'EXECUTED','CANCELLED'}
            assert order.state=='SETTLED' and order.pnl=='0.25'
        state=client.get('/api/manual-signals/status').json()
        assert state['signals']==[] and state['list_completed']
    finally:
        main.autopilot_enabled.discard(uid);real_runtime.running.discard(uid)


def test_manual_unknown_attempt_without_official_ledger_blocks_descheduling(api):
    client,factory=api;sid,uid=add_manual_user(factory);client.cookies.set('nexora_sid',sid)
    now=datetime.now(timezone.utc)
    with factory() as s:
        s.add(db.ManualSignalRun(user_id=uid,session_number=1,status='RUNNING',account_mode='demo'))
        s.add_all([
            db.ManualSignal(user_id=uid,session_number=1,capacity_slot=1,line_number=1,
                raw_line='M15 EURUSD 19:00 CALL',market='forex',timezone_name='UTC',
                local_date=now.date().isoformat(),local_time='19:00',scheduled_at=now,
                symbol='frxEURUSD',direction='CALL',duration_minutes=15,status='UNKNOWN',
                request_key='unknown-without-ledger'),
            db.ManualSignal(user_id=uid,session_number=1,capacity_slot=2,line_number=2,
                raw_line='M15 EURUSD 19:15 PUT',market='forex',timezone_name='UTC',
                local_date=now.date().isoformat(),local_time='19:15',scheduled_at=now+timedelta(minutes=15),
                symbol='frxEURUSD',direction='PUT',duration_minutes=15,status='SCHEDULED',
                request_key='future-behind-unknown'),
        ])
        s.commit()
    try:
        response=client.post('/api/manual-signals/cancel')
        assert response.status_code==200 and response.json()['cancel_pending']
        with factory() as s:
            run=s.get(db.ManualSignalRun,uid)
            assert run.status=='CANCELLING'
            assert {row.status for row in s.query(db.ManualSignal).all()}=={'UNKNOWN','SCHEDULED'}
    finally:
        main.autopilot_enabled.discard(uid)


def test_demo_settlement_resolves_unknown_manual_signal_after_restart(api,monkeypatch):
    client,factory=api;sid,uid=add_manual_user(factory);client.cookies.set('nexora_sid',sid)
    now=datetime.now(timezone.utc)
    with factory() as s:
        run=db.ManualSignalRun(user_id=uid,session_number=1,status='CANCELLING',account_mode='demo')
        order=db.DemoOrder(user_id=uid,request_key='manual-unknown-official-settlement',active_slot=uid,
            state='UNKNOWN',contract_id='99201',symbol='frxEURUSD',direction='CALL',
            duration_minutes=15,stake='2.00')
        s.add_all([run,order]);s.flush()
        s.add_all([
            db.ManualSignal(user_id=uid,session_number=1,capacity_slot=1,line_number=1,
                raw_line='M15 EURUSD 19:00 CALL',market='forex',timezone_name='UTC',
                local_date=now.date().isoformat(),local_time='19:00',scheduled_at=now,
                symbol='frxEURUSD',direction='CALL',duration_minutes=15,status='UNKNOWN',
                request_key='manual-unknown-official-settlement',ledger_mode='demo',ledger_order_id=order.id),
            db.ManualSignal(user_id=uid,session_number=1,capacity_slot=2,line_number=2,
                raw_line='M15 EURUSD 19:15 PUT',market='forex',timezone_name='UTC',
                local_date=now.date().isoformat(),local_time='19:15',scheduled_at=now+timedelta(minutes=15),
                symbol='frxEURUSD',direction='PUT',duration_minutes=15,status='SCHEDULED',
                request_key='manual-future-after-unknown-settlement'),
        ])
        order_id=order.id;s.commit()
    monkeypatch.setattr(main,'demo_contract_status',AsyncMock(return_value={'contract':{
        'contract_id':'99201','is_sold':1,'profit':'0.25','status':'won'}}))
    try:
        main.pause_manual_sessions_after_restart()
        with factory() as s:
            assert s.get(db.ManualSignalRun,uid).status=='CANCELLING'
        asyncio.run(main._reconcile_one_order(order_id))
        asyncio.run(main.reconcile_manual_lifecycle_once())
        with factory() as s:
            run=s.get(db.ManualSignalRun,uid)
            order=s.get(db.DemoOrder,order_id)
            statuses={row.status for row in s.query(db.ManualSignal).all()}
            assert run.status=='COMPLETED'
            assert order.state=='SETTLED' and order.pnl=='0.25'
            assert statuses=={'COMPLETED','CANCELLED'}
        state=client.get('/api/manual-signals/status').json()
        assert state['list_completed'] and state['signals']==[] and state['pending_count']==0
        main.demo_contract_status.assert_awaited_once_with('mock-token','V9001','99201')
    finally:
        main.autopilot_enabled.discard(uid)
