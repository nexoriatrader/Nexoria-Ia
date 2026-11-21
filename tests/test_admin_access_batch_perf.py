"""User listing performance and access-policy parity tests."""
from datetime import timedelta
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app import access, admin_auth, db, main


def test_admin_users_uses_bounded_queries_and_preserves_access(tmp_path, monkeypatch):
    engine=create_engine(f'sqlite:///{tmp_path}/admin_perf.sqlite')
    db.Base.metadata.create_all(engine)
    factory=sessionmaker(bind=engine,expire_on_commit=False)
    monkeypatch.setattr(db,'SessionLocal',factory)
    monkeypatch.setenv('NEXORA_ADMIN_SESSION_SECRET','admin-test-secret')
    monkeypatch.setenv('NEXORA_ACCESS_CONTROL_ENABLED','true')

    now=db.utcnow()
    with factory() as sess:
        users=[db.User(deriv_account_id=f'V-PERF-{i:03d}') for i in range(100)]
        sess.add_all(users)
        sess.flush()
        sess.add_all([
            db.RobotAccess(user_id=users[0].id,access_kind='admin_exception',
                           starts_at=now,expires_at=now+timedelta(days=3)),
            db.RobotAccess(user_id=users[1].id,access_kind='admin_exception',
                           starts_at=now,expires_at=now-timedelta(days=2)),
            db.RobotAccess(user_id=users[2].id,access_kind='admin_exception',
                           starts_at=now,expires_at=now+timedelta(days=3),suspended=True),
            db.ReferralCredit(invitee_user_id=users[50].id,
                              referrer_user_id=users[0].id,credited_hours=24),
            db.ReferralCredit(invitee_user_id=users[51].id,
                              referrer_user_id=users[0].id,credited_hours=24),
        ])
        sess.commit()
        by_account={u.deriv_account_id:u.id for u in users}

    selects=[]
    def count_queries(conn,cursor,statement,params,context,executemany):
        if statement.lstrip().upper().startswith('SELECT'):
            selects.append(statement)
    event.listen(engine,'before_cursor_execute',count_queries)
    try:
        client=TestClient(main.app)
        client.cookies.set('nexora_admin_sid',admin_auth.session_token())
        response=client.get('/api/admin/access/users')
        assert response.status_code==200
        items=response.json()['items']
        assert len(items)==100
        assert len(selects)==3, f'Expected bounded reads, got {len(selects)} SELECTs'
        indexed={item['deriv_account_id']:item for item in items}
        assert indexed['V-PERF-000']['referrals']==2
        assert indexed['V-PERF-000']['access']['status']=='ACTIVE'
        assert indexed['V-PERF-001']['access']['status']=='EXPIRED'
        assert indexed['V-PERF-002']['access']['status']=='SUSPENDED'
        assert indexed['V-PERF-003']['access']['status']=='TRIAL_AVAILABLE'
        assert indexed['V-PERF-003']['referrals']==0
        assert indexed['V-PERF-000']['nexora_id']==main.nexora_user_code(by_account['V-PERF-000'])

        selects.clear()
        exact=client.get('/api/admin/access/users',params={'q':'NXR-'+str(by_account['V-PERF-000']).zfill(6)})
        assert exact.status_code==200
        assert [u['deriv_account_id'] for u in exact.json()['items']]==['V-PERF-000']
        assert len(selects)==3
        selects.clear()
        miss=client.get('/api/admin/access/users',params={'q':'unknown-no-match'})
        assert miss.status_code==200 and miss.json()['items']==[]
        assert len(selects)==1

        monkeypatch.setenv('NEXORA_ACCESS_CONTROL_ENABLED','false')
        selects.clear()
        disabled=client.get('/api/admin/access/users').json()['items']
        assert len(selects)==3
        disabled_by_account={item['deriv_account_id']:item for item in disabled}
        assert disabled_by_account['V-PERF-000']['access']['status']=='TEST_MODE'
        assert disabled_by_account['V-PERF-002']['access']['status']=='SUSPENDED'
        assert disabled_by_account['V-PERF-003']['access']['status']=='TEST_MODE'
    finally:
        event.remove(engine,'before_cursor_execute',count_queries)
        engine.dispose()


def test_access_bulk_helper_is_equivalent_to_authorization_policy(tmp_path, monkeypatch):
    engine=create_engine(f'sqlite:///{tmp_path}/access_parity.sqlite')
    db.Base.metadata.create_all(engine)
    factory=sessionmaker(bind=engine)
    now=db.utcnow()
    with factory() as sess:
        user=db.User(deriv_account_id='V-ACCESS-PARITY')
        sess.add(user)
        sess.flush()
        for enforced in ('true','false'):
            monkeypatch.setenv('NEXORA_ACCESS_CONTROL_ENABLED',enforced)
            for grant in (
                None,
                db.RobotAccess(user_id=user.id,access_kind='trial_24h',
                               starts_at=now,expires_at=now+timedelta(days=2)),
                db.RobotAccess(user_id=user.id,access_kind='trial_24h',
                               starts_at=now,expires_at=now-timedelta(days=2)),
                db.RobotAccess(user_id=user.id,access_kind='trial_24h',
                               starts_at=now,expires_at=now+timedelta(days=2),suspended=True),
            ):
                if grant is None:
                    sess.query(db.RobotAccess).filter(db.RobotAccess.user_id==user.id).delete()
                    sess.flush()
                else:
                    sess.merge(grant)
                    sess.flush()
                assert access.access_status(sess,user.id,now=now)==access.access_status_for_grant(
                    sess.get(db.RobotAccess,user.id),now=now)
    engine.dispose()
