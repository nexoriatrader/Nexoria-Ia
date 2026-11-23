"""Run the actual browser handlers against a tiny no-network DOM stub.

These checks only read static HTML and isolate JS. They cannot buy contracts.
"""
from pathlib import Path
import shutil
import subprocess

import pytest

HTML = (Path(__file__).resolve().parents[1] / "app/index.html").read_text(
    encoding="utf-8"
)


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js unavailable")
def test_research_widget_and_saved_draft_inspection_fail_closed():
    script = "async function nxCheckResearchQuality(){" + HTML.split(
        "async function nxCheckResearchQuality(){", 1)[1].split(
        "let nxWizardLoadToken=", 1)[0]
    runtime = r"""
const nodes = new Map();
function nxGet(id){
  if(!nodes.has(id))nodes.set(id,{
   textContent:'',hidden:false,disabled:false,children:[],
   replaceChildren(){this.children=[];this.textContent=''},
   append(el){this.children.push(el)},
   querySelector(){return {disabled:false}}
  });
  return nodes.get(id);
}
const document = {
  createElement(){return {textContent:'',setAttribute(k,v){this[k]=v}}}
};
let serverResponse,requests=[];
async function req(path){
 requests.push(path);
 if(path.includes('/api/autopilot/')||path.includes('/api/demo/')||
    path.includes('/api/real/')||path.includes('/buy'))
   throw Error('Financial endpoint forbidden');
 return serverResponse;
}
"""
    checks = r"""
(async()=>{
 serverResponse={
   read_only:true,execution_allowed:false,orders_sent:0,
   modifies_robot_settings:false,probability_validated:false,
   report_kind:'PUBLIC_RESEARCH_CONTEXT_NOT_TRADING_SIGNALS',
   state:'CURRENT_PUBLIC_RESEARCH',
   items:[{
     market:'derived',symbol:'1HZ100V',review_state:'RESEARCH_READY',
     evidence_completeness_points:100,
     metric_kind:'DATA_COMPLETENESS_NOT_PROBABILITY',
     observation_fingerprint:'a'.repeat(64)
   }]
 };
 await nxCheckResearchQuality();
 const panel=nxGet('nxResearchQualityItems');
 if(panel.children.length!==1 || !panel.children[0].textContent.includes('Completude 100/100'))
  throw Error('Valid public research item was not shown');
 if(panel.children[0].textContent.includes('100% de acerto'))
  throw Error('Evidence was mislabeled as trading probability');
 serverResponse={...serverResponse,orders_sent:1};
 await nxCheckResearchQuality();
 if(panel.children.length!==0||!nxGet('nxResearchQualityNote').textContent.includes('não foi confirmado'))
  throw Error('Untrusted research was exposed');
 serverResponse={
   kind:'SAVED_DRAFT_EDUCATIONAL_PREVIEW',mode:'manual',
   read_only:true,applied_to_executor:false,broker_contacted:false,
   market_data_used:false,results_simulated:false,orders_sent:0,
   predicted_outcome:null,
   stages:[
      {key:'preferences',label:'Preferências salvas',status:'SAVED_DRAFT'},
      {key:'diary',label:'Agenda de estudo',status:'SCHEDULE_ONLY'},
      {key:'execution',label:'Não executa',status:'NOT_CONNECTED'},
   ],notices:['Somente observação']
 };
 await nxInspectSavedDraft();
 if(!nxGet('nxSavedDraftPreview').textContent.includes('Somente observação'))
  throw Error('Saved draft result missing');
 serverResponse={...serverResponse,market_data_used:true};
 await nxInspectSavedDraft();
 if(!nxGet('nxSavedDraftPreview').textContent.includes('não foi confirmado'))
  throw Error('Preview accepted untrusted execution claim');
 if(requests.length!==4||requests.some(p=>p.includes('/buy')))
  throw Error('Unexpected request from preview');
})().catch(err=>{console.error(err);process.exitCode=1});
"""
    proc = subprocess.run(
        ["node", "-e", runtime + script + checks],
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert proc.returncode == 0, proc.stderr
