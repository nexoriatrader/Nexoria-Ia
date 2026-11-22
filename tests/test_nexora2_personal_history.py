"""Per-user NEXORA 2.0 study history using pre-existing research storage."""
import json
from datetime import timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine,event,select
from sqlalchemy.orm import sessionmaker

from app import db,main
from app.nexora2_personal_history import (
    HISTORY_LIMIT, personal_research_key, list_personal_visits, record_personal_visit,
)
from app.research_retention import cleanup_research_reports, KEEP_LATEST

from app.research_persistence import HistoricalResearchReport, RESEARCH_KEY
from app.security import encrypt,hashsecret


def setup_db(tmp_path,monkeypatch):
    engine=create_engine(f'sqlite:///{tmp_path}/visits.sqlite')
    db.Base.metadata.create_all(engine)
    sessions=sessionmaker(bind=engine,expire_on_commit=False)
    monkeypatch.setattr(db,'SessionLocal',sessions)
    with sessions() as s:
        a=db.User(deriv_account_id='V-RESEARCH-USER-A')
        b=db.User(deriv_account_id='V-RESEARCH-USER-B')
        s.add_all([a,b]);s.flush()
        ids=(a.id,b.id)
        for suffix,uid in [('a1',a.id),('a2',a.id),('b1',b.id)]:
            s.add(db.Session(session_hash=hashsecret('research-'+suffix),
                user_id=uid,token_encrypted=encrypt('test-dummy-token'),
                expires_at=db.utcnow()+timedelta(days=2)))
        s.commit()
    return engine,sessions,ids


def test_history_authenticated_isolated_and_survives_new_session(tmp_path,monkeypatch):
    engine,sessions,(user_a,user_b)=setup_db(tmp_path,monkeypatch)
    try:
        a=TestClient(main.app)
        assert a.get('/api/nexora2/research/history').status_code==401
        assert a.post('/api/nexora2/research/history',json={'kind':'intelligence'}).status_code==401
        a.cookies.set('nexora_sid','research-a1')
        assert a.get('/api/nexora2/research/history').json()['items']==[]
        invalid=a.post('/api/nexora2/research/history',json={'kind':'real'})
        assert invalid.status_code==422
        first=a.post('/api/nexora2/research/history',json={'kind':'intelligence'})
        assert first.status_code==200
        assert first.json()['recorded'] is True
        saved=first.json()['item']
        assert saved['kind']=='intelligence' and saved['scope']=='OWN_RESEARCH_VISITS'
        assert a.get('/api/nexora2/research/history').json()['items']==[saved]
        repeat=a.post('/api/nexora2/research/history',json={'kind':'intelligence'})
        assert repeat.status_code==200 and repeat.json()['recorded'] is False
        assert len(a.get('/api/nexora2/research/history').json()['items'])==1

        b=TestClient(main.app)
        b.cookies.set('nexora_sid','research-b1')
        assert b.get('/api/nexora2/research/history').json()['items']==[]
        assert b.post('/api/nexora2/research/history',json={'kind':'strategies'}).json()['recorded'] is True
        assert len(b.get('/api/nexora2/research/history').json()['items'])==1

        # New browser client, same on-disk DB, same own-user history.
        new_a=TestClient(main.app)
        new_a.cookies.set('nexora_sid','research-a2')
        assert new_a.get('/api/nexora2/research/history').json()['items']==[saved]
        with sessions() as s:
            rows=s.scalars(select(HistoricalResearchReport)).all()
            assert len(rows)==2
            assert {r.research_key for r in rows}=={
                personal_research_key(user_a),personal_research_key(user_b)}
            for row in rows:
                contents=json.loads(row.payload_json)
                assert contents['orders_sent']==0
                assert contents['source']=='NEXORA2_USER_STUDY_VISIT'
                assert 'token' not in row.payload_json
        assert len(personal_research_key(user_a))<=60
    finally:
        engine.dispose()


def test_history_bounded_and_never_changes_global_research(tmp_path,monkeypatch):
    engine,sessions,(user_a,_)=setup_db(tmp_path,monkeypatch)
    try:
        with sessions() as s:
            now=db.utcnow()
            global_report=HistoricalResearchReport(research_key=RESEARCH_KEY,
                created_at=now,period_from=now,period_to=now,
                payload_json=json.dumps({'research':'existing global report'}))
            s.add(global_report);s.commit()
            global_id=global_report.id
        # Pre-seed >10 visits; a single write caps only this tenant's own rows.
        with sessions() as s:
            now=db.utcnow()-timedelta(minutes=30)
            for idx in range(HISTORY_LIMIT+4):
                stamp=now+timedelta(seconds=idx*10)
                s.add(HistoricalResearchReport(
                    research_key=personal_research_key(user_a),
                    created_at=stamp,period_from=stamp,period_to=stamp,
                    payload_json=json.dumps({
                        'kind':'strategies','source':'NEXORA2_USER_STUDY_VISIT','orders_sent':0})))
            s.commit()
            written=record_personal_visit(s,user_a,'intelligence')
            assert written['recorded'] is True
            items=list_personal_visits(s,user_a)
            assert len(items)==HISTORY_LIMIT
            assert items[0]['kind']=='intelligence'
            assert s.get(HistoricalResearchReport,global_id).payload_json==json.dumps({
                'research':'existing global report'})
            assert s.scalar(select(HistoricalResearchReport.id).where(
                HistoricalResearchReport.research_key==RESEARCH_KEY))==global_id
    finally:
        engine.dispose()


def test_history_read_is_select_only_and_research_paths_untouched(tmp_path,monkeypatch):
    engine,sessions,_=setup_db(tmp_path,monkeypatch)
    observed=[]
    def on_query(conn,cursor,statement,params,context,executemany):
        observed.append(statement.lstrip().split(' ',1)[0].upper())
    event.listen(engine,'before_cursor_execute',on_query)
    try:
        client=TestClient(main.app)
        client.cookies.set('nexora_sid','research-a1')
        result=client.get('/api/nexora2/research/history')
        assert result.status_code==200
        assert observed and set(observed)=={'SELECT'},observed
    finally:
        event.remove(engine,'before_cursor_execute',on_query)
        engine.dispose()


def test_history_ui_does_not_trade_or_fake_financial_results():
    html=(Path(__file__).resolve().parents[1]/'app'/'nexora2.html').read_text()
    assert 'id="personalResearchHistory"' in html
    assert 'id="personalHistoryEntries"' in html
    assert "checkedResearch('/api/nexora2/research/history')" in html
    assert "method:'POST',credentials:'same-origin'" in html
    assert "void savePersonalResearchVisit('intelligence')" in html
    assert "void savePersonalResearchVisit('strategies')" in html
    assert 'Não registra operações, resultados financeiros' in html
    for prohibited in ('/api/demo/buy','/api/real/buy','/api/autopilot/start',
                       '/api/autopilot/stop','START_REAL_AUTOPILOT'):
        assert prohibited not in html


def test_fast_switch_between_studies_keeps_both_visits(tmp_path,monkeypatch):
    engine,sessions,(user_a,_)=setup_db(tmp_path,monkeypatch)
    try:
        with sessions() as session:
            first=record_personal_visit(session,user_a,"intelligence")
            repeat=record_personal_visit(session,user_a,"intelligence")
            switched=record_personal_visit(session,user_a,"strategies")
            assert first["recorded"] is True
            assert repeat["recorded"] is False
            assert switched["recorded"] is True
            assert [item["kind"] for item in list_personal_visits(session,user_a)] == [
                "strategies", "intelligence"
            ]
    finally:
        engine.dispose()


def test_research_retention_batches_personal_keys_without_reading_large_payloads(
        tmp_path,monkeypatch):
    """Preserve old per-key retention rules with 1 SELECT for any user count."""
    engine,sessions,(user_a,user_b)=setup_db(tmp_path,monkeypatch)
    now=db.utcnow()
    with sessions() as session:
        # 12 old visits: keep newest 3 regardless of age.
        for idx in range(12):
            stamp=now-timedelta(days=15)+timedelta(minutes=idx)
            session.add(HistoricalResearchReport(
                research_key=personal_research_key(user_a),
                created_at=stamp,period_from=stamp,period_to=stamp,
                payload_json=json.dumps({"kind":"intelligence",
                   "source":"NEXORA2_USER_STUDY_VISIT","orders_sent":0})))
        # 12 recent visits: keep 10, drop only 2 excess entries.
        for idx in range(12):
            stamp=now-timedelta(hours=1)+timedelta(minutes=idx)
            session.add(HistoricalResearchReport(
                research_key=personal_research_key(user_b),
                created_at=stamp,period_from=stamp,period_to=stamp,
                payload_json=json.dumps({"kind":"strategies",
                   "source":"NEXORA2_USER_STUDY_VISIT","orders_sent":0})))
        # Unrelated report is preserved.
        session.add(HistoricalResearchReport(
            research_key=RESEARCH_KEY,created_at=now,
            period_from=now,period_to=now,
            payload_json=json.dumps({"research":"global","opaque":"x"*1000})))
        session.commit()
    statements=[]
    def observe(conn,cursor,statement,params,context,executemany):
        statements.append(statement)
    event.listen(engine,"before_cursor_execute",observe)
    try:
        deleted=cleanup_research_reports(now=now,session_factory=sessions)
        assert deleted == (12-KEEP_LATEST)+(12-HISTORY_LIMIT)
        selects=[s for s in statements if s.lstrip().upper().startswith("SELECT")]
        deletes=[s for s in statements if s.lstrip().upper().startswith("DELETE")]
        assert len(selects)==1, statements
        assert "payload_json" not in selects[0].lower()
        assert len(deletes)==1, statements
        assert "historical_research_reports" in deletes[0]
        assert not any(s.lstrip().upper().startswith(("CREATE","ALTER","DROP"))
                       for s in statements), statements
        with sessions() as session:
            by_key={}
            for key in (personal_research_key(user_a),
                        personal_research_key(user_b),RESEARCH_KEY):
                by_key[key]=session.scalars(
                    select(HistoricalResearchReport.id)
                    .where(HistoricalResearchReport.research_key==key)).all()
            assert len(by_key[personal_research_key(user_a)])==KEEP_LATEST
            assert len(by_key[personal_research_key(user_b)])==HISTORY_LIMIT
            assert len(by_key[RESEARCH_KEY])==1
    finally:
        event.remove(engine,"before_cursor_execute",observe)
        engine.dispose()


def test_personal_history_never_modifies_broker_models_or_schemas():
    from inspect import getsource
    from app import nexora2_personal_history,research_retention
    for module in (nexora2_personal_history,research_retention):
        code=getsource(module)
        for disallowed in (
            "demo_buy(", "buy_real_contract(", "demo_orders",
            "real_orders", "CREATE TABLE", "ALTER TABLE", "create_all(",
        ):
            assert disallowed not in code


def test_malformed_study_rows_are_ignored_not_returned_to_users(tmp_path,monkeypatch):
    engine,sessions,(user_a,_)=setup_db(tmp_path,monkeypatch)
    try:
        with sessions() as session:
            now=db.utcnow()
            session.add(HistoricalResearchReport(
                research_key=personal_research_key(user_a),
                created_at=now,period_from=now,period_to=now,
                payload_json=json.dumps({
                    "kind":["not-hashable"],"source":"NEXORA2_USER_STUDY_VISIT"})))
            session.commit()
            assert list_personal_visits(session,user_a)==[]
            result=record_personal_visit(session,user_a,"intelligence")
            assert result["recorded"] is True
            assert [item["kind"] for item in list_personal_visits(session,user_a)]==[
                "intelligence"
            ]
    finally:
        engine.dispose()
