"""Read-only, jargon-free dashboard UI contract: no broker execution changes."""
from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess

HTML = (Path(__file__).resolve().parents[1] / "app" / "index.html").read_text(
    encoding="utf-8"
)


class _IndexTags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.ids = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        self.tags.append((tag, attributes))
        if "id" in attributes:
            self.ids.append(attributes["id"])


def test_robot_dashboard_plain_language_overview_is_accessible_and_read_only():
    parsed = _IndexTags()
    parsed.feed(HTML)
    assert len(parsed.ids) == len(set(parsed.ids)), "Duplicate DOM identifiers"
    for element_id in (
        "robotEasyOverview", "robotEasyTitle", "robotEasyState",
        "robotEasyStateHelp", "robotEasyMode", "robotEasyModeHelp",
        "robotEasyScan", "robotEasyScanHelp", "robotEasyUpdated",
        "robotEasyRefresh", "robotEasyFeedback",
    ):
        assert parsed.ids.count(element_id) == 1, element_id

    summary = HTML.split('id="robotEasyOverview"', 1)[1].split(
        'class="card dash-control-card"', 1
    )[0]
    assert "Como está o robô agora?" in summary
    assert "Em funcionamento:" in summary
    assert "Pausado:" in summary
    assert "Interrompido:" in summary
    assert "Não foi possível confirmar:" in summary
    assert "não envia ordens" in summary
    assert "role=\"status\"" in summary
    assert 'aria-live="polite"' in summary
    assert 'aria-labelledby="robotEasyTitle"' in HTML
    refresh_buttons = [
        attrs for tag, attrs in parsed.tags
        if tag == "button" and attrs.get("id") == "robotEasyRefresh"
    ]
    assert len(refresh_buttons) == 1
    assert refresh_buttons[0]["type"] == "button"
    assert refresh_buttons[0]["onclick"] == "refreshRobotEasyOverview()"
    assert "onclick=\"toggleRobot()\"" not in summary
    assert "onclick=\"start" not in summary

    assert "@media(max-width:720px)" in HTML
    assert ".robot-easy-grid{grid-template-columns:1fr" in HTML
    assert "function renderRobotEasyOverview(status)" in HTML
    assert "function reportRobotEasyUncertainty()" in HTML
    assert "function refreshRobotEasyOverview()" in HTML
    assert "renderRobotEasyOverview(status);" in HTML
    assert "reportRobotEasyUncertainty();" in HTML
    assert "Não presuma que o robô foi desligado." in HTML
    assert "const status=await req('/api/status')" in HTML
    assert "const sequence=++dashboardStatusSequence" in HTML


def test_existing_controls_and_server_gates_are_unchanged_in_rendered_ui():
    assert 'id="robotButton"' in HTML
    assert "async function toggleRobot()" in HTML
    assert "if(!health.real_orders_enabled)" in HTML
    assert "wasRunning?'/api/autopilot/stop':'/api/autopilot/start'" in HTML
    assert "if(sequence!==dashboardStatusSequence)return null;" in HTML
    block = HTML.split("function renderRobotEasyOverview(status)", 1)[1].split(
        "function renderScanMonitor(status)", 1
    )[0]
    assert "/api/autopilot/start" not in block
    assert "/api/autopilot/stop" not in block
    assert "fetch(" not in block
    assert "POST" not in block


def test_dashboard_inline_script_syntax_is_valid_when_node_is_present():
    if not shutil.which("node"):
        return
    scripts = re.findall(r"<script(?:\\s[^>]*)?>(.*?)</script>", HTML, re.DOTALL)
    assert scripts, "Page must contain inline scripts"
    for script in scripts:
        result = subprocess.run(
            ["node", "--check"], input=script, text=True,
            capture_output=True, check=False, timeout=15,
        )
        assert result.returncode == 0, result.stderr



def test_robot_easy_connection_history_is_bounded_and_accurate():
    parsed = _IndexTags()
    parsed.feed(HTML)
    assert parsed.ids.count("robotEasyConnection") == 1
    assert parsed.ids.count("robotEasyHistory") == 1
    assert parsed.ids.count("robotEasyHistoryCount") == 1
    assert 'id="robotEasyConnection"' in HTML
    assert 'role="status">Conexão: aguardando' in HTML
    assert "A lista é temporária e desaparece quando a página é fechada" in HTML
    assert "const robotEasyChecks=[];" in HTML
    assert "if(robotEasyChecks.length>6)robotEasyChecks.length=6;" in HTML
    assert "recordRobotEasyCheck('ok',labels[state]);" in HTML
    assert "recordRobotEasyCheck('uncertain'," in HTML
    assert "if(sequence!==dashboardStatusSequence)return null;" in HTML
    block = HTML.split("const robotEasyChecks=[];", 1)[1].split(
        "function renderScanMonitor(status){", 1
    )[0]
    for forbidden in ("localStorage", "sessionStorage", "fetch(",
                      "/api/autopilot/start", "/api/autopilot/stop",
                      "POST", "sendBeacon", "document.cookie"):
        assert forbidden not in block


def test_robot_easy_status_history_javascript_runtime():
    if not shutil.which("node"):
        return
    block = "const robotEasyChecks=[];" + HTML.split(
        "const robotEasyChecks=[];", 1
    )[1].split("function renderScanMonitor(status){", 1)[0]
    runtime = r"""
const elements=new Map();
function stub(){
 return {
  textContent:'',dataset:{},disabled:false,children:[],
  classList:{add(){},remove(){}},
  replaceChildren(){this.children=[];},
  appendChild(item){this.children.push(item);}
 };
}
const $=id=>{
 if(!elements.has(id))elements.set(id,stub());
 return elements.get(id);
};
const document={createElement(){return stub();}};
"""
    checks = r"""
const status={
 running:false,interrupted_after_restart:false,auto_buy_enabled:true,
 configured_account_mode:'demo',scan_progress:{last_checked_at:new Date().toISOString()}
};
for(let i=0;i<8;i++)renderRobotEasyOverview(status);
if(robotEasyChecks.length!==6)throw Error('History not limited to six');
if($('robotEasyHistory').children.length!==6)throw Error('Displayed history is wrong');
if($('robotEasyConnection').dataset.state!=='ok')throw Error('Connection was not marked verified');
if($('robotEasyState').textContent!=='Pausado')throw Error('Expected paused state');
if(robotEasyChecks.some(item=>item.label!=='Pausado'))throw Error('Unexpected history record');
reportRobotEasyUncertainty();
if(robotEasyChecks.length!==6)throw Error('Error history must stay bounded');
if(robotEasyChecks[0].kind!=='uncertain')throw Error('Failure must be listed');
if($('robotEasyConnection').dataset.state!=='uncertain')throw Error('Failure must be visible');
if($('robotEasyState').textContent!=='Pausado')throw Error('Failure changed last known state');
if(!$('robotEasyFeedback').textContent.includes('Não presuma'))
 throw Error('Missing uncertainty warning');
if($('robotEasyHistory').children.length!==6)throw Error('List not updated');
if(Object.keys(robotEasyChecks[0]).sort().join(',')!=='kind,label,time')
 throw Error('History leaked extra fields');
"""
    result = subprocess.run(
        ["node", "-e", runtime + block + checks],
        text=True, capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr
