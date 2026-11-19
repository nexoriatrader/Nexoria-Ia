"""Exercise the actual inline dashboard JS against a synthetic failed GET, then recovery.

Runs locally under Node when installed. No browser, no Deriv API, no trading calls.
"""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_dashboard_preserves_state_after_network_error_then_recovers():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js unavailable")
    html = (Path(__file__).resolve().parents[1] / "app" / "index.html").read_text(
        encoding="utf-8"
    )
    assert html.count("async function refreshDashboard(){") == 1
    source = "async function refreshDashboard(){" + html.split(
        "async function refreshDashboard(){", 1
    )[1].split("\nlet robotStartDialogResolver=", 1)[0]
    assert source.rstrip().endswith("}")

    harness = r"""
    const assert = require('node:assert/strict');
    let dashboardStatusSequence=0;
    let robotRunning=true;
    let robotConfiguredMode='demo';
    let demoOrders=[{symbol:'R_50'}];
    let shouldFail=true;
    const requests=[];
    const elements=new Map();
    function $(id) {
      if(!elements.has(id)){
        const css={toggle(){},add(){},remove(){}};
        elements.set(id,{
          textContent:'',disabled:false,dataset:{},style:{},
          classList:css,parentElement:{classList:css},
          setAttribute(){},
        });
      }
      return elements.get(id);
    }
    async function req(path) {
      requests.push(path);
      assert.equal(path,'/api/status');
      if(shouldFail)throw new TypeError('Synthetic connection closed');
      return {
        running:false,auto_buy_enabled:true,
        autopilot_version:'V2_PULSO',v2_available:false,
        reason:'Aguardando análise',interrupted_after_restart:false,
      };
    }
    function renderScanMonitor(){}
    function renderRealtimeOrders(){}
    function showRobotNotice(){}
    function automationRadio(){return 'pulso_v2';}
    function fail(){}
    async function loadRobotAccess(){}
    async function loadDemoBalance(){}
    async function loadDemoOrders(){}
    async function loadRealBalance(){}
    async function loadRiskSession(){}
    async function loadAutopilotReadiness(){}
    // FUNCTION_UNDER_TEST
    (async()=>{
      const initial=await refreshDashboard();
      assert.equal(initial,null);
      assert.equal(robotRunning,true,'network error must not fake a robot stop');
      assert.match($('feedState').textContent,/Último estado do robô preservado/);
      shouldFail=false;
      const recovered=await refreshDashboard();
      assert.equal(recovered.running,false);
      assert.equal(robotRunning,false,'server response becomes authoritative on recovery');
      assert.equal($('robotState').textContent,'PRONTO');
      assert.equal($('robotButton').disabled,false);
      assert.deepEqual(requests,['/api/status','/api/status']);
      process.stdout.write('SYNTHETIC_DASHBOARD_RECOVERY_OK\n');
    })().catch(err=>{console.error(err);process.exitCode=1});
    """
    script = harness.replace("// FUNCTION_UNDER_TEST", source)
    done = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=15
    )
    assert done.returncode == 0, done.stderr or done.stdout
    assert "SYNTHETIC_DASHBOARD_RECOVERY_OK" in done.stdout
