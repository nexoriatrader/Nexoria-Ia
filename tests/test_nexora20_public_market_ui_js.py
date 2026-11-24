"""Execute actual NEXORA Deriv public data UI reader in a no-network Node sandbox."""
from pathlib import Path
import shutil
import subprocess

import pytest

HTML = (Path(__file__).resolve().parents[1] / "app/index.html").read_text(
    encoding="utf-8")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node unavailable")
def test_market_data_ui_rejects_untrusted_data_and_never_requests_financial_routes():
    script = "async function nxCheckMarketData(){" + HTML.split(
        "async function nxCheckMarketData(){", 1)[1].split(
        "async function nxInspectSavedDraft(){", 1)[0]
    runtime = r"""
const nodes=new Map();
function nxGet(key){
 if(!nodes.has(key))nodes.set(key,{value:'',textContent:'',disabled:false});
 return nodes.get(key);
}
nxGet('nxPublicCandleSymbol').value='R_100';
nxGet('nxPublicCandleFrame').value='60';
let received,requests=[];
async function req(path){
 requests.push(path);
 if(path.includes('buy')||path.includes('start')||
    path.includes('/api/demo/')||path.includes('/api/real/'))
  throw Error('financial request forbidden');
 if(received instanceof Error)throw received;
 return received;
}
const complete={
 report_kind:'PUBLIC_OHLC_DESCRIPTIVE_QUALITY_NOT_TRADING_SIGNALS',
 origin_path:'DERIV_PUBLIC_CLIENT_OR_SHARED_PUBLIC_CACHE',
 source_independently_certified:false,
 symbol:'R_100',granularity_seconds:60,requested_closed_candles:60,
 read_only:true,execution_allowed:false,orders_sent:0,
 trade_recommendation:null,predicted_outcome:null,
 quality:'COMPLETE',data_available:true,received_closed_candles:60,
 missing_intervals:0,last_closed_epoch:2000000000,
 descriptive_statistics:{
   first_close:100.5,last_close:101.5,minimum_close:98.5,
   maximum_close:102.5,observed_candles:60
 },snapshot_sha256:'a'.repeat(64)
};
(async()=>{
 received=complete;await nxCheckMarketData();
 if(!nxGet('nxPublicCandleResult').textContent.includes('60 velas fechadas'))
  throw Error('Complete public window not shown');
 received={...complete,orders_sent:1};await nxCheckMarketData();
 if(!nxGet('nxPublicCandleResult').textContent.includes('Dados indisponíveis'))
  throw Error('Untrusted execution response accepted');
 received={...complete,quality:'GAPPED',data_available:false,
  descriptive_statistics:null,missing_intervals:2};
 await nxCheckMarketData();
 if(!nxGet('nxPublicCandleResult').textContent.includes('intervalos ausentes: 2'))
  throw Error('Missing intervals not reported');
 received=new Error('Deriv feed down');await nxCheckMarketData();
 if(!nxGet('nxPublicCandleResult').textContent.includes('Dados indisponíveis'))
  throw Error('Feed outage concealed');
 nxGet('nxPublicCandleSymbol').value='../wrong';await nxCheckMarketData();
 if(requests.length!==4 || requests.some(p=>!p.startsWith('/api/intelligence20/market/quality?')))
  throw Error('Unexpected request from public research panel');
 if(nxGet('nxPublicCandleCheck').disabled)
  throw Error('Button stayed disabled after failure');
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    proc = subprocess.run(
        ["node", "-e", runtime + script],
        capture_output=True, text=True, timeout=15,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
