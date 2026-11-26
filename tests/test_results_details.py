"""Official result details, rolling three-day window and M1 recovery timing."""
from datetime import datetime,timedelta,timezone
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app import db,main
from app.demo_ledger import public_order
from test_demopilot import prepare,cleanup


def order(user_id,index,created_at,state='SETTLED',pnl='1.50'):
    return db.DemoOrder(user_id=user_id,request_key='history-'+str(index),
        state=state,active_slot=None,symbol='1HZ100V',direction='CALL',
        duration_minutes=5,stake='2.00',pnl=pnl,created_at=created_at)


def test_result_details_are_factual_and_brasilia_time():
    o=order(1,1,datetime(2026,11,12,15,42,13,tzinfo=timezone.utc))
    details=main.order_result_details(o)
    assert details['account_mode']=='demo'
    assert details['started_at_brasilia'] is None  # Never invent Deriv purchase time.
    assert details['time_source']=='NOT_REPORTED'
    o.broker_purchase_at=o.created_at
    assert main.order_result_details(o)['started_at_brasilia']=='2026-11-12T12:42:13-03:00'
    assert main.order_result_details(o)['time_source']=='DERIV'
    assert details['outcome']=='WIN'
    assert details['progression']['source']=='manual'
    assert main.order_result_details(order(1,2,o.created_at,pnl='-2'))['outcome']=='LOSS'
    assert main.order_result_details(order(1,3,o.created_at,pnl='0'))['outcome']=='DRAW'
    assert main.order_result_details(order(1,4,o.created_at,state='CONFIRMED',pnl=None))['outcome']=='PENDING'
    assert main.order_result_details(o,was_automatic=True)['progression']['martingale'] is None
    p=db.DemoOrderProgression(order_id=1,user_id=1,cycle_index=2,gale_index=1,
         cycle_multiplier='2.5',gale_multiplier='2.3',account_mode='demo')
    details=main.order_result_details(o,p,True)
    assert details['progression']['cycle']==2
    assert details['progression']['martingale']==1
    assert details['progression']['cycle_multiplier']=='2.5'
    assert details['progression']['gale_multiplier']=='2.3'


def test_results_api_only_last_72h_and_user_scope(tmp_path,monkeypatch):
    client,factory,engine,uid=prepare(tmp_path,monkeypatch)
    now=datetime.now(timezone.utc)
    try:
        with factory() as session:
            session.add(order(uid,1,now-timedelta(hours=71)))
            session.add(order(uid,2,now-timedelta(hours=73)))
            session.add(order(uid,3,now-timedelta(minutes=1),state='CONFIRMED',pnl=None))
            session.commit()
        response=client.get('/api/demo/orders')
        assert response.status_code==200,response.text
        data=response.json()
        assert data['retention_days']==3
        assert data['timezone']=='America/Sao_Paulo'
        assert len(data['items'])==2
        assert {r['request_key'] for r in []}==set() # no private request keys exposed
        assert data['items'][0]['outcome']=='PENDING'
        with factory() as session:
            assert session.query(db.DemoOrder).filter(db.DemoOrder.user_id==uid).count()==3
    finally:cleanup(engine)


def test_recovery_only_in_next_candle_opening_window():
    settled=datetime(2026,11,12,15,0,54,tzinfo=timezone.utc)
    assert main.m1_recovery_window_open(settled,datetime(2026,11,12,15,0,59,tzinfo=timezone.utc)) is False
    assert main.m1_recovery_window_open(settled,datetime(2026,11,12,15,1,4,tzinfo=timezone.utc)) is True
    assert main.m1_recovery_window_open(settled,datetime(2026,11,12,15,1,12,tzinfo=timezone.utc)) is False
    assert main.m1_recovery_window_open(settled,datetime(2026,11,12,15,2,3,tzinfo=timezone.utc)) is True


def test_results_table_contains_official_columns_and_autorefresh():
    from pathlib import Path
    html=(Path(__file__).resolve().parents[1]/'app'/'index.html').read_text()
    section=html.split('<section id="results"',1)[1].split('<section id="settings"',1)[0]
    for title in ('Data da compra (Deriv · Brasília)', 'Horário da compra (Deriv)',
                  'Conta', 'Ativo', 'Direção', 'Entrada', 'Expiração',
                  'Etapa', 'Situação', 'WIN / LOSS', 'Resultado'):
        assert title in section
    assert 'id="ordersBody"' in section
    assert 'id="resultsLiveStatus"' in section
    assert 'setInterval(async()=>{' in html
    assert "activePage==='dashboard'||activePage==='manual-dashboard'?7000:3000" in html
    assert 'America/Sao_Paulo' in html
    assert "p.cycle===null" in html
    assert "p.martingale===null" in html
