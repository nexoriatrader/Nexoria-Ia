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


def test_robot_easy_diagnostic_reports_safe_statuses_without_commands():
    parser = _IndexTags()
    parser.feed(HTML)
    for field in ("robotEasyDiagnosis", "robotEasyDiagnosisTitle",
                  "robotEasyDiagnosisExplanation"):
        assert parser.ids.count(field) == 1
    assert "O que a análise está mostrando?" in HTML
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
    panel = HTML.split('<section id="dashboard" class="hidden nx-neo-dashboard">', 1)[1].split(
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
