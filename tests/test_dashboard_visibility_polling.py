"""Test only read-only UI polling, without running app workers or calling a broker."""
from pathlib import Path
import shutil
import subprocess

import pytest


def _html():
    return (Path(__file__).resolve().parents[1] / "app" / "index.html").read_text(encoding="utf-8")


def test_ui_timer_sources_skip_background_tabs():
    html = _html()
    assert "if(document.hidden||resultsRequestBusy)return;" in html
    assert "if(!document.hidden&&activePage==='dashboard')refreshDashboard();" in html
    assert "if(!document.hidden&&activePage==='dashboard')loadDailyResults().catch(()=>{})" in html
    # The automation worker is server-side; only read-only UI refreshes are paused.
    assert "async function toggleRobot()" in html
    assert "await req(wasRunning?'/api/autopilot/stop':'/api/autopilot/start'" in html


def test_results_poller_skips_hidden_tab_and_resumes_without_new_timer():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js unavailable")
    html = _html()
    assert html.count("function updateResultsPolling(){") == 1
    source = "function updateResultsPolling(){" + html.split(
        "function updateResultsPolling(){", 1
    )[1].split("\nfunction go(v){", 1)[0]
    harness = r"""
    const assert = require('node:assert/strict');
    const document={hidden:false};
    let activePage='results',resultsRefreshTimer=null,resultsRequestBusy=false;
    let orders=0,statusReads=0;
    const scheduled=[];
    function setInterval(callback,ms){const timer={callback,ms};scheduled.push(timer);return timer;}
    function clearInterval(timer){timer.cancelled=true;}
    function $(id){return {textContent:''};}
    async function loadDemoOrders(){orders++;}
    async function req(path){assert.equal(path,'/api/status');statusReads++;return {};}
    function renderScanMonitor(){}
    // FUNCTION_UNDER_TEST
    (async()=>{
      updateResultsPolling();
      assert.equal(scheduled.length,1);
      assert.equal(scheduled[0].ms,3000);
      document.hidden=true;
      await scheduled[0].callback();
      assert.equal(orders,0,'hidden tab must skip history GET');
      document.hidden=false;
      await scheduled[0].callback();
      assert.equal(orders,1,'visible tab must resume history GET');
      assert.equal(statusReads,0,'results view does not need dashboard status GET');
      activePage='dashboard';
      updateResultsPolling();
      assert.equal(scheduled.length,2);
      assert.equal(scheduled[0].cancelled,true);
      assert.equal(scheduled[1].ms,7000);
      document.hidden=true;
      await scheduled[1].callback();
      assert.equal(orders,1);
      assert.equal(statusReads,0);
      document.hidden=false;
      await scheduled[1].callback();
      assert.equal(orders,2);
      assert.equal(statusReads,1);
      process.stdout.write('VISIBILITY_READONLY_POLLING_OK\n');
    })().catch(err=>{console.error(err);process.exitCode=1});
    """
    script = harness.replace("// FUNCTION_UNDER_TEST", source)
    finished = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=15)
    assert finished.returncode == 0, finished.stderr or finished.stdout
    assert "VISIBILITY_READONLY_POLLING_OK" in finished.stdout
