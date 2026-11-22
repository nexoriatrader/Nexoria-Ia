"""Passive UI clarity for DEMO-vs-research without changing robot actions."""
from pathlib import Path
from html.parser import HTMLParser
ROOT=Path(__file__).resolve().parents[1]
MAIN=(ROOT/'app'/'index.html').read_text(encoding='utf-8')
WORK=(ROOT/'app'/'nexora2.html').read_text(encoding='utf-8')

class IDParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids=[]
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if "id" in attrs:self.ids.append(attrs["id"])

def test_dashboard_controls_and_server_state_still_connected():
    parser=IDParser();parser.feed(MAIN)
    for key in ('robotButton','robotState','configureRobotButton','dashboardAccessCountdown',
                'balanceMetric','realBalanceMetric','dashRealtimeState','dashScanMonitor',
                'dashAccountMode','dashServerRobotState'):
        assert parser.ids.count(key)==1,key
    assert 'onclick="toggleRobot()"' in MAIN
    assert 'onclick="go(\'robot-config\')"' in MAIN
    assert "const status=await req('/api/status');" in MAIN
    assert "robotRunning=Boolean(status.running);" in MAIN
    assert "modeIndicator.textContent=robotConfiguredMode==='demo'" in MAIN
    assert "stateIndicator.dataset.state=robotRunning?'running':'stopped';" in MAIN

def test_dashboard_added_context_is_passive():
    part=MAIN.split('<section class="dash-environment-summary"',1)[1].split('</section>',1)[0]
    assert '<button' not in part
    assert '<a ' not in part
    assert 'onclick=' not in part
    assert 'fetch(' not in part
    assert 'saldo virtual' in part

def test_workspace_explains_distinct_demo_and_research_modes():
    parser=IDParser();parser.feed(WORK)
    for key in ('tabIntelligence','tabSignals','tabStrategies','panelIntelligence',
                'panelSignals','panelStrategies','scanButton','strategyButton','signalForm'):
        assert parser.ids.count(key)==1,key
    assert 'mode-distinction' in WORK
    assert 'Robô DEMO existente' in WORK
    assert 'saldo virtual' in WORK
    assert 'Esta tela não liga nem configura esse módulo' in WORK
    assert 'Conta REAL · execução bloqueada' in WORK
    assert 'Não envia nem agenda operações' in WORK
    assert WORK.count('<small>Ordens desta consulta</small>')==2
    for forbidden in ('/api/demo/buy','/api/real/buy','/api/autopilot/start',
                      '/api/autopilot/stop'):
        assert forbidden not in WORK
