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
        '<details class="nx-neo-more" id="nxNeoMore">', 1
    )[0]
    assert "Como está o robô agora?" in summary
    assert "Em funcionamento:" in summary
    assert "Pausado:" in summary
    assert "Interrompido:" in summary
    assert "Não foi possível confirmar:" in summary
    assert "Somente consulta" in summary
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


def test_robot_easy_diagnostic_reports_safe_statuses_without_commands():
    parser = _IndexTags()
    parser.feed(HTML)
    for field in ("robotEasyDiagnosis", "robotEasyDiagnosisTitle",
                  "robotEasyDiagnosisExplanation"):
        assert parser.ids.count(field) == 1
    assert 'id="robotEasyDiagnosisTitle"' in HTML
    assert "Este diagnóstico descreve informações técnicas" in HTML
    assert "function renderRobotEasyDiagnosis(status)" in HTML
    assert "function reportRobotEasyDiagnosisUncertain()" in HTML
    assert "renderRobotEasyDiagnosis(status);" in HTML
    assert "reportRobotEasyDiagnosisUncertain();" in HTML
    block = HTML.split("function renderRobotEasyDiagnosis(status)", 1)[1].split(
        "function renderScanMonitor(status)", 1
    )[0]
    for forbidden in ("fetch(", "sessionStorage", "localStorage",
                      "/api/autopilot/start", "/api/autopilot/stop",
                      "sendBeacon", "POST", "document.cookie"):
        assert forbidden not in block
    if not shutil.which("node"):
        return
    functions = "function renderRobotEasyDiagnosis(status)" + block
    runtime = r"""
const elements=new Map();
const $=id=>{
 if(!elements.has(id))elements.set(id,{textContent:'',dataset:{}});
 return elements.get(id);
};
function scenario(status,title,kind){
 renderRobotEasyDiagnosis(status);
 if($('robotEasyDiagnosisTitle').textContent!==title)
  throw Error(title+' but got '+$('robotEasyDiagnosisTitle').textContent);
 if($('robotEasyDiagnosis').dataset.state!==kind)
  throw Error('Wrong state '+$('robotEasyDiagnosis').dataset.state);
}
const now=new Date().toISOString();
const old=new Date(Date.now()-15*60*1000).toISOString();
scenario({running:false,scan_progress:{}},'Análises não estão em andamento','paused');
scenario({running:false,interrupted_after_restart:true,scan_progress:{}},
 'Análises interrompidas','unknown');
scenario({running:true,scan_progress:{}},'Sem análise recente','stale');
scenario({running:true,scan_progress:{last_checked_at:now}},
 'Verificação sem conclusão recente','stale');
scenario({running:true,scan_progress:{last_scan_at:old}},
 'Sem análise recente','stale');
scenario({running:true,scan_progress:{last_scan_at:now,last_checked_at:now,stage:'scanning'}},
 'Verificando informações','checking');
const base={running:true,scan_progress:{last_scan_at:now,stage:'complete'}};
scenario({...base,scan_progress:{...base.scan_progress,reason_counts:{dados_indisponiveis:2}}},
 'Alguns dados não foram recebidos','checked');
scenario({...base,scan_progress:{...base.scan_progress,reason_counts:{velas_incompletas:1}}},
 'Histórico insuficiente para analisar','checked');
scenario({...base,scan_progress:{...base.scan_progress,reason_counts:{aguardando_fechamento_m1:1}}},
 'Aguardando novos dados','checked');
scenario({...base,scan_progress:{...base.scan_progress,reason_counts:{indicadores_nao_confirmados:3}}},
 'Critérios não confirmados','checked');
scenario({...base,scan_progress:{...base.scan_progress,reason_counts:{sinal_confirmado:1}}},
 'Critérios identificados na análise','checked');
scenario(base,'Verificação registrada','checked');
reportRobotEasyDiagnosisUncertain();
if($('robotEasyDiagnosis').dataset.state!=='unknown')throw Error('Uncertainty not marked');
if($('robotEasyDiagnosisTitle').textContent!=='Não foi possível confirmar')
 throw Error('Stale diagnosis still presented as verified');
if(!$('robotEasyDiagnosisExplanation').textContent.includes('desatualizadas'))
 throw Error('Missing stale-data warning');
"""
    result = subprocess.run(
        ["node", "-e", runtime + functions],
        text=True, capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_new_studio_dashboard_is_a_real_visual_template():
    parsed = _IndexTags()
    parsed.feed(HTML)
    for element_id in ("nxNeoChartBars", "nxNeoChartEmpty", "nxNeoSampleCount",
                       "nxNeoChecked", "nxNeoChartTitle", "nxNeoChartNote",
                       "nxNeoMore"):
        assert parsed.ids.count(element_id) == 1, element_id
    panel = HTML.split('<section id="dashboard" class="hidden nx-neo-dashboard nx-calm-dashboard">', 1)[1].split(
        '<section id="robot-config" class="hidden">', 1
    )[0]
    assert '<h1>Visão geral' in panel
    assert "nx-neo-metrics" in panel
    assert panel.count('class="nx-neo-metric') >= 4
    assert "Atividade do scanner" in panel
    assert "Sem leituras nesta visita" in panel
    assert "Detalhes e resultados" in panel
    assert "nx-neo-mobile-nav" in panel
    assert "Acompanhe o sistema com clareza" in panel
    assert "Sem dados simulados" in panel
    assert "robotEasyDiagnosis" in panel
    assert "robotEasyHistory" in panel
    assert "dash-control-card" in panel
    assert "dash-daily-card" in panel
    assert "dash-realtime-card" in panel
    assert "robotButton" in panel
    assert "robotStartNotice" in panel
    assert "id=\"profitMetric\"" in panel
    assert "id=\"realProfitMetric\"" in panel
    assert "id=\"dashScanMonitor\"" in panel
    assert "body:has(#dashboard:not(.hidden)) .primary-nav{display:none}" in HTML
    assert "@media(max-width:640px)" in HTML
    assert "prefers-reduced-motion:reduce" in HTML
    assert 'aria-label="Navegação rápida do painel"' in panel
    assert 'aria-label="Atualizar dados do painel"' in panel
    assert "function renderNxNeoScannerChart(status)" in HTML
    assert "renderNxNeoScannerChart(status);" in HTML
    assert "markNxNeoScannerUncertain();" in HTML
    assert "function renderRobotEasyDiagnosis(status)" in HTML
    start = HTML.index('const nxNeoScanSamples=[];')
    stop = HTML.index('function renderScanMonitor(status){', start)
    block = HTML[start:stop]
    for forbidden in ("fetch(", "POST", "localStorage", "sessionStorage",
                      "sendBeacon", "document.cookie", "/api/autopilot/start",
                      "/api/autopilot/stop"):
        assert forbidden not in block


def test_studio_chart_only_uses_recent_server_confirmed_scans():
    if not shutil.which("node"):
        return
    block = "const nxNeoScanSamples=[];" + HTML.split(
        "const nxNeoScanSamples=[];", 1
    )[1].split("function renderScanMonitor(status){", 1)[0]
    runtime = r"""
const elements=new Map();
function element(){
 return {
  textContent:'',dataset:{},children:[],style:{},hidden:false,
  title:'', attrs:{},
  replaceChildren(){this.children=[];},
  appendChild(item){this.children.push(item);},
  setAttribute(name,value){this.attrs[name]=value;}
 };
}
const $=id=>{
 if(!elements.has(id))elements.set(id,element());
 return elements.get(id);
};
const document={createElement(){return element();}};
"""
    checks = r"""
const stamp=new Date().toISOString();
renderNxNeoScannerChart({running:true,scan_progress:{}});
if(nxNeoScanSamples.length!==0||$('nxNeoChartEmpty').hidden)
 throw Error('Chart invented activity with no data');
renderNxNeoScannerChart({running:true,scan_progress:{
 last_scan_at:stamp,total:80,checked:40}});
if(nxNeoScanSamples.length!==1||$('nxNeoChartBars').children.length!==1)
 throw Error('Real scan did not render');
if($('nxNeoChecked').textContent!=='40 / 80')
 throw Error('Observed coverage missing');
if($('nxNeoChartBars').children[0].style.height!=='50.0%')
 throw Error('Bar not computed from coverage');
renderNxNeoScannerChart({running:true,scan_progress:{
 last_scan_at:stamp,total:80,checked:40}});
if(nxNeoScanSamples.length!==1)throw Error('Duplicate scan counted twice');
renderNxNeoScannerChart({running:false,scan_progress:{
 last_scan_at:new Date(Date.now()-900000).toISOString(),total:80,checked:80}});
if(nxNeoScanSamples.length!==1)throw Error('Stale or paused scan added as new');
if($('nxNeoChecked').textContent!=='—')
 throw Error('Stale count presented as current');
for(let i=0;i<14;i++){
 renderNxNeoScannerChart({running:true,scan_progress:{
  last_scan_at:new Date(Date.now()-14000+i*1000).toISOString(),
  total:20,checked:i}});
}
if(nxNeoScanSamples.length!==12)throw Error('Unbounded scan samples');
if($('nxNeoChartBars').children.length!==12)throw Error('Unbounded chart bars');
markNxNeoScannerUncertain();
if($('nxNeoChartNote').textContent!=='Consulta sem confirmação')
 throw Error('No connection failure indicator');
if($('nxNeoChecked').textContent!=='—')
 throw Error('Old coverage displayed on failure');
"""
    result = subprocess.run(
        ["node", "-e", runtime + block + checks],
        text=True, capture_output=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr



def test_command_bar_prioritizes_stop_only_and_preserves_existing_controls():
    parsed = _IndexTags()
    parsed.feed(HTML)
    for key in ("nxNeoCommandTitle", "nxNeoRunStatus", "nxNeoRunHelp",
                "nxNeoQuickStop", "nxNeoControls"):
        assert parsed.ids.count(key) == 1, key
    section = HTML.split(
        '<section id="dashboard" class="hidden nx-neo-dashboard nx-calm-dashboard">', 1
    )[1].split('<section id="robot-config" class="hidden">', 1)[0]
    assert section.index('class="nx-neo-command"') < section.index(
        'id="robotEasyOverview"'
    )
    assert section.index('id="nxNeoQuickStop"') < section.index(
        'id="robotEasyOverview"'
    )
    assert 'onclick="nxNeoStopRunningRobot()"' in section
    assert 'id="nxNeoQuickStop"' in section
    assert 'disabled aria-label="Desligar robô em funcionamento"' in section
    assert '<details class="nx-neo-controls" id="nxNeoControls">' in section
    assert section.index('<details class="nx-neo-controls"') < section.index(
        '<div class="card dash-control-card">'
    )
    assert 'id="robotButton"' in section
    assert section.count('id="robotButton"') == 1
    assert 'onclick="toggleRobot()"' in section
    assert 'id="robotState"' in section
    assert 'id="robotStartNotice"' in section
    assert 'id="balanceMetric"' in section
    assert "if(!health.real_orders_enabled)" in HTML
    assert 'function markNxNeoTopUncertain()' in HTML
    assert 'renderNxNeoTopCommand(status);' in HTML
    assert 'markNxNeoTopUncertain();' in HTML
    assert "#dashboard .nx-neo-stop[hidden]{display:none!important}" in HTML
    style_head = HTML.split("</head>", 1)[0]
    assert style_head.count("<style>") == style_head.count("</style>")


def test_stop_only_shortcut_never_calls_start_or_repeats_a_pending_command():
    start = HTML.index("function renderNxNeoTopCommand(status){")
    end = HTML.index("function renderRobotEasyOverview(status){", start)
    block = HTML[start:end]
    for forbidden in ("fetch(", "POST", "/api/autopilot/start",
                      "/api/autopilot/stop", "sendBeacon", "localStorage"):
        assert forbidden not in block
    assert "if(!robotRunning||stop.hidden||stop.disabled" in block
    assert "if(robotRunning)await toggleRobot();" in block
    if not shutil.which("node"):
        return
    runtime = r"""
const elements=new Map();
const $=id=>{
 if(!elements.has(id))elements.set(id,{
  textContent:'',dataset:{},hidden:false,disabled:false
 });
 return elements.get(id);
};
let robotRunning=false;
let stopped=0, refreshed=0;
async function toggleRobot(){
 if(!robotRunning)throw Error('Shortcut called start path');
 stopped++;
 robotRunning=false;
}
async function refreshDashboard(){refreshed++;}
"""
    checks = r"""
(async()=>{
  renderNxNeoTopCommand({running:false,auto_buy_enabled:true});
  if(!$('nxNeoQuickStop').disabled)throw Error('Stop button enabled while stopped');
  await nxNeoStopRunningRobot();
  if(stopped!==0)throw Error('Stop shortcut called with stopped process');
  robotRunning=true;
  renderNxNeoTopCommand({running:true,auto_buy_enabled:true});
  if($('nxNeoQuickStop').hidden||$('nxNeoQuickStop').disabled)
   throw Error('Stop button not available for running process');
  if($('nxNeoRunStatus').textContent!=='● Em funcionamento')
   throw Error('Wrong status shown');
  await nxNeoStopRunningRobot();
  if(stopped!==1||refreshed!==1)throw Error('Stop not dispatched once');
  await nxNeoStopRunningRobot();
  if(stopped!==1)throw Error('Repeated stop invoked');
  robotRunning=true;
  renderNxNeoTopCommand({running:true,auto_buy_enabled:true});
  $('robotButton').dataset.commandPending='true';
  renderNxNeoTopCommand({running:true,auto_buy_enabled:true});
  if(!$('nxNeoQuickStop').disabled)throw Error('Stop button ignores pending command');
  await nxNeoStopRunningRobot();
  if(stopped!==1)throw Error('Stop invoked while command pending');
  $('robotButton').dataset.commandPending='false';
  renderNxNeoTopCommand({running:true,auto_buy_enabled:true});
  markNxNeoTopUncertain();
  if($('nxNeoRunStatus').dataset.state!=='unknown')
   throw Error('Connection failure must be obvious');
  if(!$('nxNeoRunHelp').textContent.includes('desatualizado'))
   throw Error('Missing status uncertainty warning');
  robotRunning=false;
  renderNxNeoTopCommand({running:false,auto_buy_enabled:true});
  if(!$('nxNeoQuickStop').disabled)throw Error('Inactive stop became enabled');
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = subprocess.run(
        ["node", "-e", runtime + block + checks],
        capture_output=True, text=True, timeout=20, check=False
    )
    assert result.returncode == 0, result.stderr



def test_minimal_robot_home_keeps_only_two_visible_surfaces():
    """The initial dashboard must be easy to scan without losing legacy DOM bindings."""
    panel = HTML.split(
        '<section id="dashboard" class="hidden nx-neo-dashboard nx-calm-dashboard">', 1
    )[1].split('<section id="robot-config" class="hidden">', 1)[0]
    assert '<section class="nx-neo-command"' in panel
    assert 'id="nxNeoQuickStop"' in panel
    assert 'class="nx-calm-activity"' in panel
    assert 'id="nxCalmActivityText"' in panel
    assert '<details class="nx-calm-extra" id="nxCalmExtra">' in panel
    assert panel.index('class="nx-neo-command"') < panel.index('class="nx-calm-activity"')
    assert panel.index('class="nx-calm-activity"') < panel.index(
        '<details class="nx-calm-extra"'
    )
    # The activation panel is one clear disclosure in the top control banner,
    # not hidden inside another disclosure. Existing safeguards are unchanged.
    before_activity = panel.split('<section class="nx-calm-activity"', 1)[0]
    assert '<details class="nx-neo-controls" id="nxNeoControls">' in before_activity
    assert 'id="nxNeoActivationLabel"' in before_activity
    assert 'id="robotButton"' in before_activity
    assert 'id="robotStartNotice"' in before_activity
    assert '<details class="nx-neo-controls" id="nxNeoControls" open' not in panel
    assert 'Revisar e confirmar' in before_activity
    assert 'onclick="toggleRobot()"' in before_activity
    # All secondary read-only cards stay in the separate "More information" area.
    advanced = panel.split('<details class="nx-calm-extra" id="nxCalmExtra">', 1)[1]
    for element_id in ("robotEasyOverview", "nxNeoChartBars", "profitMetric",
                       "realProfitMetric", "dashScanMonitor", "nxNeoMore"):
        assert 'id="' + element_id + '"' in advanced
        assert 'id="' + element_id + '"' not in panel.split(
            '<details class="nx-calm-extra" id="nxCalmExtra">', 1
        )[0]
    assert 'aria-label="Desligar robô em funcionamento"' in panel
    assert 'onclick="nxNeoStopRunningRobot()"' in panel
    assert "function renderNxCalmActivity(status)" in HTML
    assert "function markNxCalmActivityUnknown()" in HTML
    assert "renderNxCalmActivity(status);" in HTML
    assert "markNxCalmActivityUnknown();" in HTML
    assert "if(!health.real_orders_enabled)" in HTML
    assert "wasRunning?'/api/autopilot/stop':'/api/autopilot/start'" in HTML
    html_head = HTML.split("</head>", 1)[0]
    assert html_head.count("<style>") == html_head.count("</style>")


def test_minimal_robot_activity_reports_only_confirmed_status_without_actions():
    start = HTML.index("function renderNxCalmActivity(status){")
    end = HTML.index("function renderNxNeoTopCommand(status){", start)
    block = HTML[start:end]
    for forbidden in ("fetch(", "POST", "localStorage", "sessionStorage",
                      "sendBeacon", "document.cookie", "/api/autopilot/start",
                      "/api/autopilot/stop", "toggleRobot("):
        assert forbidden not in block
    if not shutil.which("node"):
        return
    runtime = r"""
const elements=new Map();
const $=id=>{
 if(!elements.has(id))elements.set(id,{textContent:'',dataset:{}});
 return elements.get(id);
};
"""
    checks = r"""
const iso=new Date().toISOString();
function check(status,title,detail){
 renderNxCalmActivity(status);
 if($('nxCalmActivityText').textContent!==title)
  throw Error('Expected '+title+'; found '+$('nxCalmActivityText').textContent);
 if(detail&&!$('nxCalmActivityDescription').textContent.includes(detail))
  throw Error('Incorrect explanation for '+title);
 if($('nxCalmConnection').dataset.state!=='ok')
  throw Error('Confirmed status not marked');
}
check({running:false,auto_buy_enabled:true},'Sem atividade','pausado');
check({running:false,interrupted_after_restart:true},'Atividade interrompida','interrupção');
check({running:true,scan_progress:{}},'Aguardando dados','Nenhuma análise');
check({running:true,scan_progress:{last_checked_at:iso,stage:'scanning'}},
 'Analisando informações','Verificando dados');
check({running:true,scan_progress:{last_scan_at:iso,stage:'signal'}},
 'Aguardando nova análise','Última verificação');
check({running:false,auto_buy_enabled:false},'Sem atividade','não está disponível');
markNxCalmActivityUnknown();
if($('nxCalmActivityText').textContent!=='Estado não confirmado')
 throw Error('Failed request left a stale activity visible');
if($('nxCalmConnection').dataset.state!=='uncertain')
 throw Error('Failed request was presented as confirmed');
if(!$('nxCalmActivityDescription').textContent.includes('desatualizada'))
 throw Error('Missing stale-status warning');
"""
    result = subprocess.run(
        ["node", "-e", runtime + block + checks],
        capture_output=True, text=True, check=False, timeout=20
    )
    assert result.returncode == 0, result.stderr



def test_activation_link_is_top_level_and_requires_existing_confirmation():
    """A visible top shortcut only expands the original safeguarded controls."""
    panel = HTML.split(
        '<section id="dashboard" class="hidden nx-neo-dashboard nx-calm-dashboard">', 1
    )[1].split('<section id="robot-config" class="hidden">', 1)[0]
    top = panel.split('<section class="nx-calm-activity"', 1)[0]
    assert top.index('id="nxNeoQuickStop"') < top.index('id="nxNeoControls"')
    assert top.index('id="nxNeoControls"') < top.index('id="robotButton"')
    assert 'aria-label="Mostrar controles de ativação e configurações"' in top
    assert 'id="nxNeoActivationLabel">Ligar robô</strong>' in top
    assert 'Revisar e confirmar' in top
    assert 'id="robotButton" class="hero-action" disabled aria-disabled="true"' in top
    assert 'onclick="nxNeoStopRunningRobot()"' in top
    assert 'onclick="toggleRobot()"' in top
    assert "if(!await askRobotStartConfirmation(profile,risk,catalogue))return;" in HTML
    assert "if(!health.real_orders_enabled)" in HTML
    assert "if(robotRunning)await toggleRobot();" in HTML
    assert "'nxNeoActivationLabel'" in HTML
    assert "running?'Configurações':'Ligar robô'" in HTML
    assert "textContent='Verificar controles';" in HTML
    # The visual shortcut does not invent an unguarded start path.
    between = HTML.split('function renderNxNeoTopCommand(status){', 1)[1].split(
        'function renderRobotEasyOverview(status){', 1
    )[0]
    for text in ("fetch(", "/api/autopilot/start", "POST", "sendBeacon"):
        assert text not in between



def test_compact_always_visible_controls_do_not_enable_unguarded_start():
    """User sees the two states on mobile; only stopping an active process is actionable."""
    panel = HTML.split(
        '<section id="dashboard" class="hidden nx-neo-dashboard nx-calm-dashboard">', 1
    )[1].split('<section class="nx-calm-activity"', 1)[0]
    assert 'id="nxNeoStartDisplay"' in panel
    assert 'id="nxNeoQuickStop"' in panel
    assert panel.index('id="nxNeoStartDisplay"') < panel.index('id="nxNeoQuickStop"')
    assert 'id="nxNeoStartDisplay" class="nx-neo-start-display" role="note"' in panel
    assert 'aria-label="Ativação exige revisão e confirmação"' in panel
    assert 'Ligar: confirmar</span>' in panel
    assert 'id="nxNeoQuickStop" type="button" class="nx-neo-stop"' in panel
    assert 'id="nxNeoControls"' in panel
    assert '#dashboard.nx-calm-dashboard .nx-neo-compact-actions' in HTML
    assert '#dashboard.nx-calm-dashboard .nx-neo-stop:disabled' in HTML
    # The existing activation button is not cloned into the top row.
    assert panel.count('id="robotButton"') == 1
    assert 'onclick="toggleRobot()"' in panel
    top_actions = panel.split('class="nx-neo-compact-actions"', 1)[1].split('</div>', 1)[0]
    assert 'onclick="toggleRobot()"' not in top_actions
    assert 'onclick="nxNeoStopRunningRobot()"' in top_actions
    assert '#dashboard.nx-calm-dashboard .nx-neo-controls .dash-add-time' in HTML
    assert '#dashboard.nx-calm-dashboard .nx-neo-controls .dash-balance-action' in HTML
    assert "stop.hidden=false;" in HTML
    assert "stop.disabled=!running" in HTML
    assert "stop.disabled=!robotRunning" in HTML
    assert "if(!robotRunning||stop.hidden||stop.disabled" in HTML
    assert "if(!health.real_orders_enabled)" in HTML
    # No new request path, no broker execution changes.
    js = HTML.split('function renderNxNeoTopCommand(status){', 1)[1].split(
        'function renderRobotEasyOverview(status){', 1
    )[0]
    for forbidden in ("fetch(", "/api/autopilot/start", "POST"):
        assert forbidden not in js



def test_mobile_controls_no_fake_start_and_no_overflow():
    """Regression for the mobile screenshot: no duplicate fake button or stretched grid."""
    panel = HTML.split(
        '<section id="dashboard" class="hidden nx-neo-dashboard nx-calm-dashboard">', 1
    )[1].split('<section class="nx-calm-activity"', 1)[0]
    top = panel.split('<details class="nx-neo-controls"', 1)[0]
    assert 'id="nxNeoStartDisplay"' in top
    assert 'role="note"' in top
    assert 'id="nxNeoStartDisplay" type="button"' not in top
    assert 'id="nxNeoQuickStop"' in top
    assert 'onclick="nxNeoStopRunningRobot()"' in top
    assert panel.count('id="robotButton"') == 1
    assert panel.count('id="nxNeoAccountDetails"') == 1
    controls = panel.split('<details class="nx-neo-controls"', 1)[1]
    assert 'class="nx-neo-account"' in controls
    assert "Informações de saldo" in controls
    assert controls.index('class="nx-neo-account"') < controls.index(
        'id="balanceMetric"'
    )
    assert '<details class="nx-neo-account" id="nxNeoAccountDetails" open' not in panel
    assert 'id="robotButton"' in controls
    assert "if(!await askRobotStartConfirmation(profile,risk,catalogue))return;" in HTML
    assert "if(!health.real_orders_enabled)" in HTML
    css = HTML.split("</head>", 1)[0]
    assert "/* Calm hotfix: remove fake start button" in css
    assert "display:flex!important;flex-direction:column" in css
    assert "width:auto!important;height:auto!important" in css
    assert "display:flex!important;flex-wrap:wrap" in css
    assert "grid-template-columns:minmax(0,1fr)!important" in css
    assert ".nx-neo-account .dash-balance-grid" in css
    assert css.count("<style>") == css.count("</style>")
    parser = _IndexTags()
    parser.feed(HTML)
    assert len(parser.ids) == len(set(parser.ids))
