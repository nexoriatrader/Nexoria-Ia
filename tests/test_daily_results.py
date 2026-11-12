"""Result of the day resets at 00:00 America/Sao_Paulo (UTC-03 currently)."""
from datetime import datetime,timezone
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app import db,main
from pathlib import Path


def test_brasilia_midnight_windows_cross_utc_date():
    before=datetime(2026,11,12,2,59,59,tzinfo=timezone.utc)
    after=datetime(2026,11,12,3,0,0,tzinfo=timezone.utc)
    start1,end1,local1=main.daily_pnl_window(before)
    start2,end2,local2=main.daily_pnl_window(after)
    assert local1.date().isoformat()=='2026-11-11'
    assert local2.date().isoformat()=='2026-11-12'
    assert start1.isoformat()=='2026-11-11T03:00:00+00:00'
    assert start2.isoformat()=='2026-11-12T03:00:00+00:00'


def test_daily_pnl_is_settled_only_user_scoped_and_not_limited_to_100(tmp_path,monkeypatch):
    engine=create_engine('sqlite:///'+str(tmp_path/'daily.sqlite'))
    db.Base.metadata.create_all(engine)
    factory=sessionmaker(bind=engine,expire_on_commit=False)
    monkeypatch.setattr(db,'SessionLocal',factory)
    def make_order(user_id,index,stamp,pnl,state='SETTLED'):
        return db.DemoOrder(user_id=user_id,request_key='day-'+str(user_id)+'-'+str(index),
            state=state,symbol='1HZ100V',direction='CALL',duration_minutes=5,
            stake='2.00',pnl=str(pnl),updated_at=stamp)
    old=datetime(2026,11,12,2,59,59,tzinfo=timezone.utc)
    new=datetime(2026,11,12,3,0,0,tzinfo=timezone.utc)
    later=datetime(2026,11,12,18,15,0,tzinfo=timezone.utc)
    instant=datetime(2026,11,12,19,0,0,tzinfo=timezone.utc)
    try:
        with factory() as session:
            u=db.User(deriv_account_id='VDAILY');v=db.User(deriv_account_id='VOTHER')
            session.add_all([u,v]);session.flush()
            session.add(make_order(u.id,0,old,100))
            session.add(make_order(u.id,1,new,'-2.50'))
            session.add(make_order(u.id,2,later,'1.75'))
            session.add(make_order(u.id,3,later,900,'CONFIRMED'))
            session.add(make_order(v.id,4,later,999))
            # Deliberately go over UI's 100-row history cap.
            for idx in range(5,110):
                session.add(make_order(u.id,idx,later,'0.10'))
            session.commit()
            result=main.today_demo_pnl(session,u.id,instant)
            assert Decimal(result['pnl'])==Decimal('9.75')
            assert result['date']=='2026-11-12'
            assert result['timezone']=='America/Sao_Paulo'
            assert result['next_reset_at']=='2026-11-13T00:00:00-03:00'
            next_day=main.today_demo_pnl(session,u.id,datetime(2026,11,13,3,0,tzinfo=timezone.utc))
            assert next_day['pnl']=='0.00'
            prev_day=main.today_demo_pnl(session,u.id,datetime(2026,11,12,2,59,59,tzinfo=timezone.utc))
            assert prev_day['pnl']=='100.00'
    finally:
        engine.dispose()


def test_dashboard_hides_win_loss_and_uses_official_daily_route():
    html=(Path(__file__).resolve().parents[1]/'app'/'index.html').read_text()
    dashboard=html.split('<section id="dashboard"',1)[1].split('<section id="results"',1)[0]
    assert 'Resultado do dia' in dashboard
    assert 'Desde 00h · Brasília' in dashboard
    assert 'Vitórias / Derrotas' not in dashboard
    assert 'id="scoreMetric"' not in dashboard
    assert 'Resultado acumulado' not in dashboard
    assert 'id="balanceMetric"' in dashboard
    assert 'id="realBalanceMetric"' in dashboard
    assert 'id="ordersBody"' in html
    assert '/api/demo/results/today' in html
    assert 'new Date(result.next_reset_at)' in html
