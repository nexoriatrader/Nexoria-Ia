"""Guided NEXORA configuration: educational copy and step navigation contract.

Tests forbid modifications to backend order execution and verify the existing
form controls survive the UI rearrangement. No live trading is performed.
"""
from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess

SOURCE = (Path(__file__).resolve().parents[1] / "app" / "index.html").read_text(
    encoding="utf-8"
)


class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.tags = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append((tag, attrs))
        if "id" in attrs:
            self.ids.append(attrs["id"])


def config_source():
    return SOURCE.split('<section id="robot-config" class="hidden">', 1)[1].split(
        '</section>', 1
    )[0]


def test_guided_configuration_has_six_visible_steps_and_preserves_legacy_group():
    panel = config_source()
    parsed = Tags()
    parsed.feed(SOURCE)
    assert len(parsed.ids) == len(set(parsed.ids))
    assert panel.count('class="auto-step"') == 7
    assert panel.count('data-nx-wizard="') == 6
    assert 'data-nx-wizard="1"' not in panel
    assert 'data-nx-wizard="2" aria-current="step">Conta' in panel
    assert 'Etapa 1 de 6' in panel
    assert 'Vamos conhecer as configurações?' not in panel
    assert 'if(number===1){group.hidden=true;group.inert=true;}' in SOURCE
    assert 'class="nx-guide-current"' in panel
    assert 'id="autoWizardTitle"' in panel
    assert 'id="autoWizardHint"' in panel
    assert 'id="autoWizardProgress" role="status" aria-live="polite"' in panel
    assert 'id="autoWizardTrack" role="progressbar"' in panel
    assert 'aria-valuemax="6" aria-valuenow="1"' in panel
    assert "Um passo de cada vez" in panel
    assert "Navegar ou salvar preferências não liga o robô" in panel
    assert "Treinamento usa saldo fictício" in SOURCE
    assert 'id="autoWizardBack"' in panel
    assert 'id="autoWizardNext"' in panel
    assert 'class="nx-guide-save-note"' in panel
    assert 'id="autoConfigSummary"' in panel


def test_advanced_and_new_options_are_retained_without_becoming_default():
    panel = config_source()
    for name in ("autoMarket", "autoMode", "autoStrategy"):
        assert f'name="{name}"' in panel
    for field in (
        "autoRealAccount", "automationSymbols", "autoDirection",
        "autoStake", "autoWin", "autoLoss", "autoPayout", "autoMg",
        "autoMgSettings", "autoCycles", "autoCycleSettings", "autoCycleMultiplier",
        "autoSaveButton", "autoConfigFeedback",
    ):
        assert f'id="{field}"' in panel
    assert 'id="autoAdvancedOptions"' in panel
    assert '<details class="nx-guide-advanced" id="autoAdvancedOptions">' in panel
    assert 'Opções avançadas e de alto risco' in panel
    assert 'id="autoMg"' in panel.split('id="autoAdvancedOptions"', 1)[1].split('</details>', 1)[0]
    assert 'id="autoCycles"' in panel.split('id="autoAdvancedOptions"', 1)[1].split('</details>', 1)[0]
    assert 'id="autoIndicators"' not in panel
    assert "Indicadores selecionados" not in panel
    assert "O que significam os indicadores?" not in panel
    assert 'id="learningIndicatorsSection"' not in SOURCE
    assert 'input type="radio" name="autoMode" value="demo" checked' in panel
    assert 'input type="radio" name="autoMode" value="real"' in panel
    assert 'onclick="saveAutomationProfile()"' in panel
    assert 'onclick="go(\'dashboard\')"' in panel
    assert 'onclick="changeAutomationWizardStep(-1)"' in panel
    assert 'onclick="changeAutomationWizardStep(1)"' in panel
    assert 'onclick="applyAutomationPreset(\'conservative\')"' in panel
    assert 'onclick="applyAutomationPreset(\'aggressive\')"' in panel
    assert 'onclick="applyAutomationPreset(\'custom\')"' in panel


def test_visual_skin_scoped_and_broker_guards_remain():
    header = SOURCE.split("</head>", 1)[0]
    assert header.count("<style>") == header.count("</style>")
    assert "/* Guided settings information layer" in header
    assert "#robot-config .nx-guide-path" in header
    assert "#robot-config .nx-guide-advanced" in header
    assert "#robot-config .nx-guide-explainer" in header
    assert "max-width:600px" in header
    assert "prefers-reduced-motion:reduce" in header
    # No transaction handling or broker endpoints introduced by the visual CSS.
    skin = header.split("/* Guided settings information layer", 1)[1].split(
        "</style>", 1
    )[0]
    for forbidden in ("fetch(", "onclick=", "/api/autopilot/start",
                      "/api/autopilot/stop"):
        assert forbidden not in skin
    for path in ("/api/automation/profile", "/api/onboarding/config",
                 "/api/autopilot/start", "/api/autopilot/stop"):
        assert path in SOURCE
    assert "if(!await askRobotStartConfirmation(profile,risk,catalogue))return;" in SOURCE
    assert "if(!health.real_orders_enabled)" in SOURCE


def test_navigation_updates_only_visual_progress_with_correct_bounds():
    if not shutil.which("node"):
        return
    source = SOURCE.split("const automationGuideDescriptions=[", 1)[1].split(
        "function changeAutomationWizardStep(delta){", 1
    )[0]
    script = "const automationGuideDescriptions=[" + source
    runtime = r"""
let autoWizardStep=2;
const stepElements=Array.from({length:7},(_,i)=>({
  dataset:{wizardStep:String(i+1)},hidden:false
}));
const stepMarkers=Array.from({length:6},(_,i)=>({
  dataset:{nxWizard:String(i+2)},
  attrs:{},setAttribute(k,v){this.attrs[k]=v},
  removeAttribute(k){delete this.attrs[k]}
}));
const nodes=new Map();
const $=id=>{
 if(!nodes.has(id))nodes.set(id,{
  textContent:'',hidden:false,style:{},attrs:{},
  setAttribute(k,v){this.attrs[k]=v}
 });
 return nodes.get(id);
};
const document={
 querySelectorAll(q){
  if(q==='.auto-wizard-step')return stepElements;
  if(q==='[data-nx-wizard]')return stepMarkers;
  throw Error('Unexpected selector '+q);
 }
};
"""
    checks = r"""
setAutomationWizardStep(4);
if(autoWizardStep!==4)throw Error('Invalid current step');
if(stepElements.filter(x=>!x.hidden).length!==1 ||stepElements[3].hidden)
 throw Error('More than one visible step');
if($('autoWizardTrack').attrs['aria-valuenow']!=='3')throw Error('ARIA not updated');
if(stepMarkers[2].attrs['aria-current']!=='step')throw Error('Current step not announced');
if(stepMarkers[3].attrs['aria-current'])throw Error('Next step incorrectly current');
if($('autoWizardTitle').textContent!=='Veja os ativos disponíveis')
 throw Error('Incorrect description');
setAutomationWizardStep(7);
if(!$('autoWizardNext').hidden || $('autoWizardBack').hidden)
 throw Error('End of guide controls incorrect');
setAutomationWizardStep(0);
if(autoWizardStep!==2 || !$('autoWizardBack').hidden ||
 $('autoWizardNext').hidden)throw Error('Lower clamp failed');
if(!stepElements[0].hidden || stepElements[1].hidden)throw Error('Intro must stay hidden');
if($('autoWizardProgress').textContent!=='Etapa 1 de 6')throw Error('First step progress');
if(stepMarkers[0].attrs['aria-current']!=='step')throw Error('Conta not current');
setAutomationWizardStep(500);
if(autoWizardStep!==7 || $('autoWizardProgress').textContent!=='Etapa 6 de 6')throw Error('Upper clamp failed');
"""
    result = subprocess.run(
        ["node", "-e", runtime + script + checks],
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_review_explains_existing_parameters_without_saving_automatically():
    summary = SOURCE.split("function renderAutomationSummary(){", 1)[1].split(
        "async function loadAutomationProfile(){", 1
    )[0]
    for expression in (
        "Conta:", "Mercado:", "Ativos:", "Análise:",
        "Direção:", "Valor por entrada:",
        "Limites registrados:", "Payout mínimo informado:",
        "Opções avançadas:", "Treinamento · saldo fictício",
    ):
        assert expression in summary
    assert "Indicadores complementares:" not in summary
    for forbidden in ("fetch(", "req(", "POST", "saveAutomationProfile("):
        assert forbidden not in summary
    assert "node.textContent=[" in summary
    assert "].join('\\n');" in summary


def test_manual_mode_reuses_risk_settings_but_skips_strategy_asset_steps():
    panel = config_source()
    assert 'value="strategies" checked' in panel
    assert 'Nexora IA Estratégias' in panel
    assert 'Nexora Manual (lista de sinais)' in panel
    assert 'operation_mode:manual?\'manual\':\'strategies\'' in SOURCE
    assert "if(operationMode()!=='manual')check('automationSymbols'" in SOURCE
    profile_loader = SOURCE.split("async function loadAutomationProfile(){", 1)[1].split(
        "async function saveAutomationProfile(){", 1
    )[0]
    manual_load = profile_loader.split("if(operationMode()==='manual'){", 1)[1].split(
        "setAutomationRadio('autoMarket'", 1
    )[0]
    assert 'automationAssets=[];automationSelected.clear()' in manual_load
    assert 'req(\'/api/automation/catalogue?' not in manual_load
    assert 'autoStake' in panel and 'autoWin' in panel and 'autoLoss' in panel
    assert 'id="autoMg"' in panel and 'id="autoCycles"' in panel
    assert 'data-wizard-step="3"' in SOURCE
    assert 'data-wizard-step="4"' in SOURCE
    assert 'data-wizard-step="5"' in SOURCE


def test_manual_signal_card_uses_readonly_preview_and_explicit_queue_transitions():
    assert 'id="manualSignalsCard"' in SOURCE
    for field in ('manualMarket', 'manualDay', 'manualTimezone', 'manualSignalText'):
        assert f'id="{field}"' in SOURCE
    assert 'value="America/Sao_Paulo" selected>Brasília (UTC−03:00) · padrão' in SOURCE
    assert 'M15 é a duração do contrato a partir da aceitação da compra' in SOURCE
    assert 'Cada sinal só é tentado no minuto indicado' in SOURCE
    assert 'sinais vencidos nunca são recuperados' in SOURCE
    assert "state:'/api/manual-signals/status'" in SOURCE
    assert "preview:'/api/manual-signals/preview'" in SOURCE
    assert "confirm:'/api/manual-signals/confirm'" in SOURCE
    assert "body:JSON.stringify({...manualSignalsPreview.payload,idempotency_key:idempotency.key})" in SOURCE
    assert "MANUAL_IDEMPOTENCY_STORAGE_KEY='nexora.manual.confirmation-keys.v1'" in SOURCE
    assert 'localStorage.getItem(MANUAL_IDEMPOTENCY_STORAGE_KEY)' in SOURCE
    assert 'localStorage.setItem(MANUAL_IDEMPOTENCY_STORAGE_KEY,JSON.stringify(entries))' in SOURCE
    assert 'item.local_date&&item.local_time' in SOURCE
    assert 'item.timezone_name,marketLabel' in SOURCE
    assert 'SCHEDULE_DEMO_MANUAL_SIGNALS' not in SOURCE
    assert "req('/api/autopilot/start'" in SOURCE
    assert "req('/api/autopilot/stop',{method:'POST'})" in SOURCE
    assert 'Sinais pendentes serão preservados na fila' in SOURCE
    assert 'contratos já enviados continuarão na Deriv; nada será apagado ou cancelado' in SOURCE
    assert 'id="manualCancelButton"' in SOURCE and '>DESAGENDAR LISTA</button>' in SOURCE
    assert "cancel:'/api/manual-signals/cancel'" in SOURCE
    assert 'function cancelManualSignals()' in SOURCE
    assert 'manualActivateButton' not in SOURCE
    assert 'activateManualSignals' not in SOURCE
    assert 'Reativar execução pausada' not in SOURCE
    assert "if(typeof manualProfileMode!=='undefined'&&manualProfileMode){showRobotNotice('Este perfil usa Nexora Manual." in SOURCE
    assert 'state?.persisted_status||state?.status||state?.run_status' in SOURCE
    assert 'const listComplete=Boolean(state?.list_completed||run===\'COMPLETED\')' in SOURCE
    assert "manualPauseButton').textContent='DESLIGAR ROBÔ'" in SOURCE
    assert 'LISTA CONCLUÍDA' in SOURCE
    assert 'a operação atual continuará até a liquidação oficial' in SOURCE
    assert 'Desligar o robô Nexora Manual?' in SOURCE
