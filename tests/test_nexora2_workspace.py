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


def test_workspace_signal_parser_and_brasilia_date(tmp_path):
    """Exercise the browser's actual parsing logic without a live broker or DOM."""
    import re
    import shutil
    import subprocess
    import pytest
    node=shutil.which('node')
    if not node:
        pytest.skip('Node.js not available')
    html=WORKSPACE.read_text(encoding='utf-8')
    script=re.search(r'<script>(.*?)</script>',html,flags=re.S).group(1)
    assert script.rstrip().endswith('initNexora2();')
    script=script.replace('initNexora2();','')
    script+="""
const good=validStudySignal('M15 EUR/JPY CALL 19:00');
if(!good||good.timeframe!=='M15'||good.asset!=='EUR/JPY'||
   good.direction!=='CALL'||good.hour!=='19:00')process.exit(13);
if(validStudySignal('M99 EUR/JPY CALL 25:00')!==null)process.exit(14);
if(validStudySignal('M15 EUR/JPY CALL 19:61')!==null)process.exit(15);
if(validStudySignal('M15 EUR/JPY BUY 19:00')!==null)process.exit(16);
if(!/^\\d{4}-\\d{2}-\\d{2}$/.test(brasiliaToday()))process.exit(17);
if(brasiliaDate(new Date('2026-02-28T15:00:00-03:00'))!=='2026-02-28')process.exit(18);
"""
    js=tmp_path/'parser.js'
    js.write_text(script,encoding='utf-8')
    outcome=subprocess.run([node,str(js)],capture_output=True,text=True,timeout=15)
    assert outcome.returncode==0,outcome.stderr


def test_workspace_study_details_and_session_history_js(tmp_path):
    """Validate summaries, bounds and no silent re-labelling as financial results."""
    import re
    import shutil
    import subprocess
    import pytest
    node=shutil.which('node')
    if not node:
        pytest.skip('Node.js not available')
    html=WORKSPACE.read_text(encoding='utf-8')
    script=re.search(r'<script>(.*?)</script>',html,flags=re.S).group(1)
    script=script.replace('initNexora2();','')
    script+="""
const good=validationStats({duration_minutes:5,
  validation:{signals:20,wins:12,losses:8,hit_rate:.6},enough_validation_samples:true});
if(good.interval!=='5 min'||good.sample!=='20'||good.rate!=='60,0%'||
  good.quality!=='Amostra mínima atingida')process.exit(31);
const empty=validationStats({validation:{signals:0,hit_rate:null},enough_validation_samples:false});
if(empty.sample!=='0'||empty.rate!=='Sem resultados'||
  !empty.quality.includes('insuficiente'))process.exit(32);
const invalid=validationStats({duration_minutes:1,
  validation:{signals:20,hit_rate:1.3},enough_validation_samples:true});
if(invalid.rate!=='Sem resultados')process.exit(33);
const cleaned=cleanResearchHistory([
  {kind:'intelligence',at:'2026-11-21T02:00:00Z',assets:3,state:'RESEARCH_ONLY'},
  {kind:'real',at:'2026-11-21T02:00:00Z',assets:3,state:'RESEARCH_ONLY'},
  {kind:'strategies',at:'invalid',assets:3,state:'RESEARCH_ONLY'}
]);
if(cleaned.length!==1||cleaned[0].assets!==3)process.exit(34);
"""
    js=tmp_path/'details-history.js'
    js.write_text(script,encoding='utf-8')
    result=subprocess.run([node,str(js)],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr


def test_workspace_readonly_details_and_history_ui():
    html=WORKSPACE.read_text(encoding='utf-8')
    for ident in ('scanDetails','scanDetailsRows','strategyDetails','strategyDetailsRows',
                  'studyHistory','historyEntries','historyStatus'):
        assert f'id="{ident}"' in html
    assert 'appendResearchHistory(' in html
    assert 'sessionStorage' in html and 'localStorage' not in html
    assert 'Não é histórico PostgreSQL' in html
    assert 'Não mede lucro, payout, latência' in html
    assert 'function validationStats(row)' in html
    assert "item=>showResearchDetails('scan',item)" in html
    assert "item=>showResearchDetails('strategy',item)" in html
    assert "fetch('/api/demo/buy'" not in html
    assert "fetch('/api/real/buy'" not in html
