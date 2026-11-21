"""NEXORA 2.0 same-site research workspace: authenticated, non-executing."""
from datetime import timedelta
from pathlib import Path
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import db,main
from app.security import encrypt,hashsecret


WORKSPACE=Path(__file__).resolve().parents[1]/'app'/'nexora2.html'
MAIN_UI=Path(__file__).resolve().parents[1]/'app'/'index.html'


def test_workspace_is_protected_and_does_not_trade(tmp_path,monkeypatch):
    engine=create_engine(f'sqlite:///{tmp_path}/workspace.sqlite')
    db.Base.metadata.create_all(engine)
    factory=sessionmaker(bind=engine,expire_on_commit=False)
    monkeypatch.setattr(db,'SessionLocal',factory)
    with factory() as sess:
        user=db.User(deriv_account_id='V-WORKSPACE-TEST',selected_mode='demo')
        sess.add(user);sess.flush()
        sess.add(db.Session(session_hash=hashsecret('workspace_sid'),user_id=user.id,
                            token_encrypted=encrypt('fake-token'),
                            expires_at=db.utcnow()+timedelta(days=1)))
        sess.commit()
    try:
        client=TestClient(main.app)
        response=client.get('/nexora2')
        assert response.status_code==401
        client.cookies.set('nexora_sid','workspace_sid')
        response=client.get('/nexora2')
        assert response.status_code==200
        assert 'no-store' in response.headers.get('cache-control','')
        assert 'NEXORA Intelligence 2.0' in response.text
        assert 'Meus sinais' in response.text
        assert 'Minhas estratégias' in response.text
        assert 'Conta REAL · execução bloqueada' in response.text
        assert '/api/research/asset-scanner?version=1' in response.text
        assert '/api/research/strategy-lab' in response.text
    finally:
        engine.dispose()


def test_workspace_is_linked_to_original_site_without_executing_orders():
    existing=MAIN_UI.read_text(encoding='utf-8')
    html=WORKSPACE.read_text(encoding='utf-8')
    assert 'href="/nexora2"' in existing
    assert 'href="/"' in html
    assert 'role="tablist"' in html
    for forbidden in ('/api/autopilot/start','/api/autopilot/stop',
                      '/api/demo/buy','/api/real/buy',
                      'START_REAL_AUTOPILOT','START_DEMO_AUTOPILOT'):
        assert forbidden not in html
    assert 'sessionStorage' in html
    assert 'localStorage' not in html
    assert 'Não há agendamento no servidor' in html


def test_workspace_inline_script_compiles(tmp_path):
    import re
    import shutil
    import subprocess
    import pytest
    html=WORKSPACE.read_text(encoding='utf-8')
    match=re.search(r'<script>(.*?)</script>',html,flags=re.S)
    assert match
    node=shutil.which('node')
    if not node:
        pytest.skip('Node.js not available')
    script=tmp_path/'workspace-inline.js'
    script.write_text(match.group(1),encoding='utf-8')
    result=subprocess.run([node,'--check',str(script)],
                          capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
