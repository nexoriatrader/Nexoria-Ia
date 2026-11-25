from pathlib import Path


HTML = (Path(__file__).resolve().parents[1] / 'app' / 'index.html').read_text()


def test_recovery_controls_are_below_the_results_and_have_requested_labels():
    panel = HTML.split('<section class="nx-panel-history"', 1)[1].split('</section>', 1)[0]
    assert panel.index('id="nxPanelHistoryLatest"') < panel.index('id="nxRenewSession"')
    assert panel.index('id="nxPanelHistoryPrevious"') < panel.index('id="nxClearResults"')
    assert '>Renovar sessão</button>' in panel
    assert '>Limpar resultados anteriores</button>' in panel


def test_result_clear_is_local_and_does_not_hide_open_operations():
    assert 'localStorage.setItem(key,JSON.stringify([...hidden]))' in HTML
    assert "order?.state==='SETTLED'&&hidden.has(String(order.id))" in HTML
    assert "fetch('/api/orders/recover'" not in HTML
    assert "req('/api/orders/recover',{method:'POST'})" in HTML
    assert 'operações em análise continuam visíveis' in HTML


def test_recovery_button_calls_read_only_reconciliation_route():
    assert "onclick=" in HTML and 'renewPanelSession()' in HTML
    assert "req('/api/orders/recover',{method:'POST'})" in HTML
    assert 'blocked' in HTML and 'released' in HTML
