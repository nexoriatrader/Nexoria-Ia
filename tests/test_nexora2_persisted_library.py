"""Existing PostgreSQL research archive remains available through read-only APIs.

Tests explicitly check unauthorized reads, allowed research keys, bounded output,
database-backed recovery after a new session, and zero financial mutations.
"""
import json
from datetime import datetime,timedelta,timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine,event
from sqlalchemy.orm import sessionmaker

from app import db,main
from app.research_persistence import (
    HistoricalResearchReport, RESEARCH_KEY,MULTI_RESEARCH_KEY,PULLBACK_KEY
)
from app.security import encrypt,hashsecret


def _setup(tmp_path,monkeypatch):
    engine=create_engine(f'sqlite:///{tmp_path}/persistent_research.sqlite')
    db.Base.metadata.create_all(engine)
    sessions=sessionmaker(bind=engine,expire_on_commit=False)
    monkeypatch.setattr(db,'SessionLocal',sessions)
    now=datetime(2026,11,21,tzinfo=timezone.utc)
    with sessions() as session:
        user=db.User(deriv_account_id='V-RESEARCH-LIBRARY')
        session.add(user);session.flush()
        for suffix in ('a','b'):
            session.add(db.Session(session_hash=hashsecret('library-'+suffix),
                user_id=user.id,token_encrypted=encrypt('dummy-test-token'),
                expires_at=db.utcnow()+timedelta(days=2)))
        reports=[
            (RESEARCH_KEY,now-timedelta(hours=3)),
            (MULTI_RESEARCH_KEY,now-timedelta(hours=2)),
            (PULLBACK_KEY,now-timedelta(hours=1)),
            ('PRIVATE_UNRELATED_RESEARCH',now)
        ]
        for key,created in reports:
            session.add(HistoricalResearchReport(
                research_key=key,created_at=created,
                period_from=now-timedelta(days=7),period_to=now,
                payload_json=json.dumps({'candles':700,'coverage_pct':93.5,
                                         'results':[{},{}],'orders_sent':0})))
        session.commit()
    return engine,sessions


def test_persisted_archive_auth_filter_restart_and_detail(tmp_path,monkeypatch):
    engine,sessions=_setup(tmp_path,monkeypatch)
    try:
        client=TestClient(main.app)
        unauth=client.get('/api/nexora2/research/library')
        assert unauth.status_code==401
        assert client.get('/api/nexora2/research/library/1').status_code==401
        client.cookies.set('nexora_sid','library-a')
        result=client.get('/api/nexora2/research/library')
        assert result.status_code==200
        body=result.json()
        assert body['scope']=='SHARED_SYSTEM_RESEARCH'
        assert body['orders_sent']==0
        assert len(body['items'])==3
        assert body['items'][0]['title']=='Estudo de pullback M1/M3'
        assert all(item['title']!='PRIVATE_UNRELATED_RESEARCH' for item in body['items'])
        assert not any('payload_json' in item for item in body['items'])
        identity=body['items'][0]['id']
        detail=client.get(f'/api/nexora2/research/library/{identity}')
        assert detail.status_code==200
        assert detail.json()['summary']=={
            'candles':700,'model_count':2,'coverage_pct':93.5,
            'source':'PERSISTED_HISTORICAL_RESEARCH'
        }
        assert detail.json()['execution_allowed'] is False
        assert client.get('/api/nexora2/research/library/999999').status_code==404
        assert client.get('/api/nexora2/research/library/4').status_code==404

        # A different browser session and app TestClient recover stored rows.
        other=TestClient(main.app)
        other.cookies.set('nexora_sid','library-b')
        again=other.get('/api/nexora2/research/library')
        assert again.status_code==200
        assert again.json()['items']==body['items']
        with sessions() as session:
            assert session.query(HistoricalResearchReport).count()==4
    finally:
        engine.dispose()


def test_persisted_archive_never_writes_or_loads_unbounded_payload(tmp_path,monkeypatch):
    engine,sessions=_setup(tmp_path,monkeypatch)
    operations=[]
    def record(conn,cursor,statement,params,context,executemany):
        operations.append(statement.strip().split(' ',1)[0].upper())
    event.listen(engine,'before_cursor_execute',record)
    try:
        client=TestClient(main.app)
        client.cookies.set('nexora_sid','library-a')
        assert client.get('/api/nexora2/research/library').status_code==200
        assert client.get('/api/nexora2/research/library/1').status_code==200
        assert set(operations)=={'SELECT'},operations
    finally:
        event.remove(engine,'before_cursor_execute',record)
        engine.dispose()
