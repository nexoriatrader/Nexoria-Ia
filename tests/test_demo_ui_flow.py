from pathlib import Path


def load_ui():
    return (Path(__file__).resolve().parents[1]/"app"/"index.html").read_text(encoding="utf-8")


def test_demo_interface_keeps_official_balance_without_manual_buy_button():
    html=load_ui()
    assert "TESTAR UMA ENTRADA DEMO" not in html
    assert "Teste controlado de integração" not in html
    assert "testDemoBuy()" not in html
    assert "/api/demo/balance" in html
    assert "/api/real/balance" in html
    assert "Saldo conta DEMO" in html
    assert "Saldo conta REAL" in html
    assert 'id="balanceMetric"' in html
    assert 'id="realBalanceMetric"' in html


def test_official_contract_history_replaces_local_simulator():
    html=load_ui()
    assert "HISTÓRICO COMPLETO DO ROBÔ" in html
    assert "/api/demo/orders" in html
    assert 'id="ordersBody"' in html
    assert "/api/demo/results/today" in html
    for forbidden in ("LIGAR SIMULAÇÃO", "Histórico de simulação", "Resultado virtual",
                      "/api/start", "/api/stop", "/api/history"):
        assert forbidden not in html


def test_mobile_navigation_and_single_primary_robot_control():
    html=load_ui()
    assert 'id="primaryNavigation"' in html
    assert 'id="robotButton"' in html
    assert 'id="configureRobotButton"' in html
    assert "AutoPilot" in html
    assert "loadDemoOrders()" in html
    assert "/api/onboarding/config" in html
    assert 'data-view="results"' in html


def test_continuity_prompt_clarifies_demo_not_real():
    root=Path(__file__).resolve().parents[1]
    text=(root/"PROMPT_MANUS_DEMO_SEM_BLOQUEIOS_DESNECESSARIOS.md").read_text(encoding="utf-8")
    assert "NEXORA_DEMO_BUY_ENABLED=true" in text
    assert "Não executar compras reais" in text


def test_real_mode_requires_explicit_authorization_before_purchases():
    html=load_ui()
    assert 'name="autoMode" value="real"' in html
    assert 'id="autoRealAccount"' in html
    assert "/api/accounts/available" in html
    assert "health.real_orders_enabled" in html
    assert "START_REAL_AUTOPILOT" in html
    assert "askRobotStartConfirmation(profile,risk,catalogue)" in html


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


def test_landing_preserves_explicit_deriv_oauth_and_no_auto_start():
    html=load_ui()
    welcome=html.split('<section id="welcome"',1)[1].split('<section id="setup"',1)[0]
    assert 'href="/auth/deriv/start"' in welcome
    assert 'id="lpHeroTrialButton"' in welcome
    assert 'onclick="startNexoraTrial(event)"' in welcome
    assert 'data-trial-label>24 horas</span>' in welcome
    assert 'Conexão oficial Deriv' in welcome
    assert 'Como funciona' in welcome
    assert 'Operações financeiras envolvem riscos' in welcome
    assert '/api/autopilot/start' not in welcome
    assert 'id="robotButton"' in html


def test_premium_landing_mobile_and_accessibility():
    html=load_ui()
    assert 'aria-labelledby="landingHeading"' in html
    assert '@media(max-width:680px)' in html
    assert '@media(prefers-reduced-motion:reduce)' in html
    assert '.landing-cta:focus-visible' in html
    assert 'id="landingHeading"' in html
    assert 'id="strategies"' in html


def test_authenticated_oauth_requires_initial_setup_before_user_starts_robot():
    html=load_ui()
    assert 'id="setupForm"' in html
    assert 'id="setupStake"' in html
    assert 'id="setupLoss"' in html
    assert "const loginResult=params.get('login');" in html
    assert "go('robot-config');" in html
    assert "go('setup')" not in html
    assert "page('dashboard')" in html
    assert 'history.replaceState' in html
    assert 'id="robotButton"' in html


def test_real_account_selection_is_separate_from_order_authorization():
    html=load_ui()
    config=html.split('<section id="robot-config"',1)[1].split('<section id="results"',1)[0]
    assert 'name="autoMode" value="demo"' in config
    assert 'name="autoMode" value="real"' in config
    assert 'id="autoRealAccount"' in config
    assert "saveAutomationProfile()" in config
    assert "/api/demo/buy" not in config
    assert "/api/autopilot/start" not in config
    assert "changeAutomationAccount" in html
    assert "real_account_id" in html
    assert "START_REAL_AUTOPILOT" in html


def test_autopilot_activation_confirmation_is_mode_specific_and_explicit():
    html=load_ui()
    assert 'id="robotStartDialog"' in html
    assert 'role="dialog"' in html
    assert 'id="robotStartDialogDetails"' in html
    assert 'id="robotStartDialogWarning"' in html
    assert "DINHEIRO REAL" in html
    assert "profile.real_account_id" in html
    assert "askRobotStartConfirmation(profile,risk,catalogue)" in html
    assert "resolveRobotStartDialog(true)" in html
    assert "START_REAL_AUTOPILOT" in html
    assert "START_DEMO_AUTOPILOT" in html


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
    assert "ACESSAR CORRETORA" in html
    assert 'id="primaryNavigation"' in html
    assert 'id="robotButton"' in html



def test_dashboard_navigation_reuses_existing_history_refresh():
    """Avoid duplicate history requests without changing the read-only poller or robot controls."""
    html=load_ui()
    navigation=html.split("function go(v){",1)[1].split("\nfunction fail(",1)[0]
    assert "if(v==='dashboard'){refreshDashboard();loadDailyResults().catch(()=>{});}" in navigation
    assert "loadDemoOrders().catch(()=>{})" not in html
    # Refreshing the dashboard already fetches the account-specific official history.
    refresh=html.split("async function refreshDashboard(){",1)[1].split(
        "\nlet robotStartDialogResolver=",1)[0]
    assert "await Promise.allSettled([loadRobotAccess(),loadDemoBalance(),loadDemoOrders()" in refresh


def test_results_view_avoids_unnecessary_full_dashboard_polling():
    """Results page keeps its dedicated history polling, not the dashboard fan-out."""
    html=load_ui()
    assert "resultsRefreshTimer=setInterval(" in html
    assert "activePage==='dashboard'?7000:3000" in html
    assert "if(activePage==='dashboard')refreshDashboard();" in html
    assert "if(activePage==='dashboard'||activePage==='results')refreshDashboard();" not in html
