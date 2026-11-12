from pathlib import Path


def load_ui():
    return (Path(__file__).resolve().parents[1]/"app"/"index.html").read_text(encoding="utf-8")


def test_demo_interface_keeps_official_balance_without_manual_buy_button():
    html=load_ui()
    assert "TESTAR UMA ENTRADA DEMO" not in html
    assert "Teste controlado de integração" not in html
    assert "testDemoBuy()" not in html
    assert "demoTestInfo" not in html
    assert "/api/demo/balance" in html
    assert "Saldo Treinamento" in html
    assert "Saldo Conta Real" in html


def test_official_contract_history_replaces_local_simulator():
    html=load_ui()
    assert "Histórico oficial" in html
    assert "/api/demo/orders" in html
    assert 'id="ordersBody"' in html  # Historical results are preserved.
    assert '/api/demo/results/today' in html  # Daily P/L computed on server.
    for forbidden in ("LIGAR SIMULAÇÃO", "Histórico de simulação", "Resultado virtual",
                      "/api/start", "/api/stop", "/api/history"):
        assert forbidden not in html


def test_mobile_navigation_and_single_primary_robot_control():
    html=load_ui()
    assert 'id="drawer"' in html
    assert 'id="menuButton"' in html
    assert 'id="robotButton"' in html
    assert 'id="chooseReal" aria-pressed="false"' in html
    assert "AutoPilot" in html
    assert "loadDemoOrders()" in html
    assert "/api/onboarding/config" in html
    assert "data-view=\"results\"" in html


def test_continuity_prompt_clarifies_demo_not_real():
    root=Path(__file__).resolve().parents[1]
    text=(root/"PROMPT_MANUS_DEMO_SEM_BLOQUEIOS_DESNECESSARIOS.md").read_text(encoding="utf-8")
    assert "NEXORA_DEMO_BUY_ENABLED=true" in text
    assert "Não executar compras reais" in text


def test_real_mode_is_explicitly_unavailable_for_purchases():
    html=load_ui()
    assert "Verificando conta real vinculada à Deriv" in html
    assert 'id="chooseReal" aria-pressed="false"' in html

def test_inline_javascript_syntax_is_valid(tmp_path):
    """Catch malformed inline JavaScript before it blocks OAuth login initialization."""
    import re
    import shutil
    import subprocess
    import pytest

    html = load_ui()
    blocks = re.findall(r"<script(?:\\s[^>]*)?>(.*?)</script>", html, flags=re.S | re.I)
    assert blocks, "The OAuth login UI must have a working initialization script"
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable for syntax checking")
    for index, block in enumerate(blocks):
        path = tmp_path / f"inline_{index}.js"
        path.write_text(block, encoding="utf-8")
        result = subprocess.run([node, "--check", str(path)],
                                capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr


def test_premium_landing_preserves_explicit_deriv_oauth_and_no_auto_start():
    html=load_ui()
    welcome=html.split('<section id="welcome"',1)[1].split('<section id="choose"',1)[0]
    assert 'CONECTAR E ACESSAR O ROBÔ' in welcome
    assert 'href="/auth/deriv/start"' in welcome
    assert 'A conexão à corretora não inicia operações automaticamente' in welcome
    assert 'Ative o AutoPilot' in welcome
    assert 'Conta real não habilitada para compras' not in welcome
    assert 'somente na conta de demonstração' not in welcome
    assert 'operações automáticas.' in welcome
    assert 'Execução automática' in welcome
    assert 'Análise automática' in welcome
    assert 'AMBIENTE DEMO' not in welcome
    assert 'DEMO SOMENTE' not in welcome
    assert 'landing-benefits' in welcome
    assert 'landing-showcase' in welcome
    assert 'landing-headline' in welcome
    assert '/api/autopilot/start' not in welcome
    assert 'landing-steps' in welcome
    assert 'id="robotButton"' in html  # Trading remains opt-in in the existing dashboard.


def test_premium_landing_mobile_and_accessibility():
    html=load_ui()
    assert 'aria-labelledby="landingHeading"' in html
    assert 'landing-backdrop" aria-hidden="true"' in html
    assert '@media(max-width:680px)' in html
    assert '@media(prefers-reduced-motion:reduce)' in html
    assert '.landing-cta:focus-visible' in html
    assert 'id="lab"' in html  # Existing strategy laboratory preserved.


def test_account_selection_after_successful_deriv_oauth():
    html=load_ui()
    assert 'id="chooseHeading"' in html
    assert 'id="chooseDemo"' in html
    assert 'selectDemoAccount()' in html
    assert "new URLSearchParams(window.location.search).get('login')==='ok'" in html
    assert "page('choose')" in html
    assert "page('dashboard')" in html
    assert "history.replaceState" in html
    assert 'id="demoInfo"' in html


def test_real_account_card_is_informational_and_cannot_trigger_orders():
    html=load_ui()
    choose=html.split('<section id="choose"',1)[1].split('<section id="dashboard"',1)[0]
    assert 'role="group"' in choose
    assert 'aria-disabled="true"' in choose
    assert 'SALDO REAL' in choose
    assert 'id="realInfo"' in choose
    assert 'onclick="selectDemoAccount()"' in choose
    assert '/api/demo/buy' not in choose
    assert '/api/autopilot/start' not in choose
    assert 'Modo de treinamento selecionado' in choose
    assert 'choose-accounts' in choose
    assert 'A um passo do' in choose
    assert 'Recomendamos utilizar o treinamento por algumas horas' in choose
    assert 'id="chooseContinue"' in choose
    assert 'onclick="selectRealAccount()"' in choose
    assert 'id="realPreview"' in html
    assert 'Nenhum contrato com saldo real será comprado' in html
    assert "if(selectedAccountMode==='real'){go('realPreview');return;}" in html
    assert 'onclick="continueAccount()"' in choose
    assert 'aria-pressed="true"' in choose
    assert 'id="chooseReal" aria-pressed="false"' in choose
    assert '@media(max-width:700px)' in html


def test_autopilot_activation_confirmation_is_clear_and_multiline():
    html=load_ui()
    assert 'window.confirm(`Você está prestes a iniciar o robô.' in html
    assert 'Confirme apenas se já revisou as configurações' in html
    assert 'clique em "Cancelar"' in html
    assert 'ATIVAR AUTOPILOT NA CONTA DERIV DEMO?' not in html
    assert 'START_DEMO_AUTOPILOT' in html


def test_network_failure_does_not_claim_autopilot_start_failed():
    html=load_ui()
    assert "button.dataset.commandPending='true'" in html
    assert "const networkError=e instanceof TypeError" in html
    assert "sem repetir o comando" in html
    assert "for(let attempt=0;attempt<4;attempt++)" in html
    assert "status.running!==wasRunning" in html
    assert "Não foi possível confirmar a " in html
    assert "await req(wasRunning?'/api/autopilot/stop':'/api/autopilot/start'" in html


def test_dashboard_drops_stale_status_responses_and_locks_pending_command():
    html=load_ui()
    assert "let dashboardStatusSequence=0;" in html
    assert "const sequence=++dashboardStatusSequence;" in html
    assert "if(sequence!==dashboardStatusSequence)return null;" in html
    assert "dataset.commandPending==='true'" in html
    assert "button.dataset.commandPending='false'" in html


def test_dashboard_ignores_stale_status_after_loading_secondary_cards():
    html=load_ui()
    beginning=html.index("async function refreshDashboard(){")
    ending=html.index("async function toggleRobot(){",beginning)
    refresh=html[beginning:ending]
    assert refresh.count("if(sequence!==dashboardStatusSequence)return null;")>=2
    assert refresh.index("await Promise.allSettled(")<refresh.rindex(
        "if(sequence!==dashboardStatusSequence)return null;")
    assert "const status=await req('/api/status')" in refresh


def test_risk_reset_confirmation_has_real_line_breaks():
    html=load_ui()
    assert "const message=`REINICIAR SESSÃO DE RISCO DEMO?\n\n" in html
    assert "Só é permitido sem contratos pendentes. Deseja continuar?`" in html


def test_header_has_official_broker_access_instead_of_demo_badge():
    html=load_ui()
    header=html.split("<header>",1)[1].split("</header>",1)[0]
    assert 'id="accountBadge"' in header
    assert 'ACESSAR CORRETORA' in header
    assert 'href="https://home.deriv.com/"' in header
    assert 'target="_blank"' in header
    assert 'rel="noopener noreferrer"' in header
    assert 'DERIV DEMO</span>' not in header
    assert "$('accountBadge').classList.toggle('hidden',!logged)" in html
