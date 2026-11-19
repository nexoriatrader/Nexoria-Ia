"""Functional UI risk-switch test: executes the actual inline JS in Node.

All account data and HTTP responses are synthetic. No browser network calls.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


def test_wizard_switches_restore_correct_saved_preferences_and_discard_stale_results(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js not available")
    html = (Path(__file__).resolve().parents[1] / "app" / "index.html").read_text()
    start = html.index("let automationRiskRequest=0,automationAccountRequest=0;")
    end = html.index("async function changeAutomationMarket()", start)
    actual_js = html[start:end]
    assert "async function restoreSelectedAutomationRisk()" in actual_js

    script = r"""
const assert = require('node:assert/strict');
const items = ['autoRealAccountWrapper','autoRealAccount','autoStake','autoWin',
 'autoLoss','autoMg','autoGales','autoMultiplier','autoCycles',
 'autoCycleMultiplier','autoConfigFeedback'];
const fields = Object.fromEntries(items.map(id=>[id,{
 value:'', checked:false, textContent:'', children:[],
 classList:{toggle(){}},
 replaceChildren(){this.children=[]},
 append(child){this.children.push(child)},
 addEventListener(event,handler){this[event]=handler}
}]));
const $ = id=>fields[id];
const document = {createElement:()=>({value:'',textContent:''})};
let selectedMode='demo';
const automationRadio=()=>selectedMode;
let automationProfileSaved=true;
let observed=[];
function renderAutomationSummary(){}
function toggleAutomationProgressions(){}
let postpone=false, resolveOld=null;
const configs={
 demo:{stake:'2.50',stop_win:'12.00',stop_loss:'8.00',martingale:false,gale_count:0,multiplier:'2.00',cycles:'0',cycle_multiplier:'2.00'},
 CRONE:{stake:'7.00',stop_win:'19.00',stop_loss:'14.00',martingale:false,gale_count:0,multiplier:'2.00',cycles:'0',cycle_multiplier:'2.00'},
 CRTWO:{stake:'3.00',stop_win:'9.00',stop_loss:'6.00',martingale:false,gale_count:0,multiplier:'2.00',cycles:'2',cycle_multiplier:'1.25'}
};
async function req(path){
 observed.push(path);
 if(path==='/api/accounts/available'){
  return {real_accounts:[{account_id:'CRONE'},{account_id:'CRTWO'}]};
 }
 const url=new URL(path,'https://test.invalid');
 const mode=url.searchParams.get('mode');
 const account=mode==='demo'?'demo':url.searchParams.get('account_id');
 assert.ok(configs[account], 'Unexpected account preview '+account);
 if(postpone && account==='CRONE'){
  return new Promise(resolve=>{resolveOld=()=>resolve(configs.CRONE)});
 }
 return configs[account];
}
""" + actual_js + r"""
(async()=>{
 selectedMode='real';
 await changeAutomationAccount('CRONE');
 assert.equal(fields.autoRealAccount.value,'CRONE');
 await restoreSelectedAutomationRisk();
 assert.equal(fields.autoStake.value,'7.00');
 assert.equal(fields.autoLoss.value,'14.00');

 // Clearing an account must not preserve financial values of the old account.
 fields.autoRealAccount.value='CRTWO';
 clearUnsavedAutomationRiskOnAccountChange();
 assert.equal(fields.autoStake.value,'');
 assert.equal(fields.autoLoss.value,'');
 await restoreSelectedAutomationRisk();
 assert.equal(fields.autoStake.value,'3.00');
 assert.equal(fields.autoWin.value,'9.00');
 assert.equal(fields.autoCycles.value,'2');

 // Switching from REAL back to DEMO restores DEMO only.
 selectedMode='demo';
 clearUnsavedAutomationRiskOnAccountChange();
 await changeAutomationAccount();
 await restoreSelectedAutomationRisk();
 assert.equal(fields.autoStake.value,'2.50');
 assert.equal(fields.autoLoss.value,'8.00');

 // A late response from CRONE must never overwrite the more recent CRTWO.
 selectedMode='real';
 await changeAutomationAccount('CRONE');
 postpone=true;
 const oldRequest=restoreSelectedAutomationRisk();
 assert.ok(resolveOld);
 fields.autoRealAccount.value='CRTWO';
 clearUnsavedAutomationRiskOnAccountChange();
 await restoreSelectedAutomationRisk();
 resolveOld();
 await oldRequest;
 assert.equal(fields.autoStake.value,'3.00');
 assert.equal(fields.autoLoss.value,'6.00');
 assert.ok(observed.some(path=>path.includes('mode=demo')));
 assert.ok(observed.some(path=>path.includes('account_id=CRONE')));
 assert.ok(observed.some(path=>path.includes('account_id=CRTWO')));
 console.log('UI_SYNTHETIC_SWITCH_AND_STALE_RESPONSE_OK');
})().catch(err=>{console.error(err);process.exit(1)});
"""
    js = tmp_path / "risk_switch.js"
    js.write_text(script, encoding="utf-8")
    result = subprocess.run([node, str(js)],capture_output=True,
                            text=True,timeout=12)
    assert result.returncode==0, result.stderr
    assert "UI_SYNTHETIC_SWITCH_AND_STALE_RESPONSE_OK" in result.stdout
