"""P0-05 isolated Chromium inspection of actual dashboard status rendering.

Runs with synthetic API responses on a loopback static server. All external
HTTP requests are blocked; no login, purchase, broker or financial operation.
"""
import base64
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from playwright.sync_api import sync_playwright


APP=Path(__file__).resolve().parents[1]/"app"

FAKE_API=r"""
(() => {
 const reply=obj=>({ok:true,status:200,json:async()=>obj});
 const confirmed={id:991,state:'CONFIRMED',contract_stage:'CONFIRMED_OPEN_UNSETTLED',
   account_mode:'real',symbol:'R_100',direction:'CALL',stake:'1.00',
   duration_minutes:5,pnl:null,contract_id:'SYNTHETIC',outcome:null};
 window.p005FailStatus=false;
 window.fetch=async (input,opts={})=>{
  const path=new URL(String(input),'https://mock.invalid').pathname;
  if(opts.method && opts.method!=='GET')throw Error('P0-05 test prohibits POST');
  if(path==='/api/session')return reply({authenticated:false,session_valid:false});
  if(path==='/api/public/offer')return reply({trial_hours:24});
  if(path==='/api/status') {
   if(window.p005FailStatus)throw Error('synthetic status timeout');
   return reply({running:true,running_now:null,state_stale:true,
     running_state:'UNKNOWN',configured_mode:'real',configured_account_mode:'real',
     configured:true,auto_buy_enabled:false,real_orders_enabled:false,
     interrupted_after_restart:false,autopilot_version:'V2_PULSO',
     v2_available:false,selected_strategy:'pulso_v2',scan_progress:null,
     reason:'Estado remoto não confirmado',
     real_capability:{configured:true,technical_feature_enabled:false,
       administrative_release_approved:false,account_authorized:true,
       running_now:false},
     contracts:{known:true,counts:{PENDING:0,UNKNOWN:0,CONFIRMED:1,SETTLED:0},
       confirmed_open_count:1,settled_count:0,open_unsettled:1}
   });
  }
  if(path==='/api/real/orders')return reply({account_mode:'real',items:[confirmed]});
  if(path==='/api/demo/orders')return reply({account_mode:'demo',items:[]});
  if(path==='/api/access/status')return reply({status:'ACTIVE',allowed:true});
  return reply({});
 };
})();
"""

def capture(page,name):
    blob=page.screenshot(type="jpeg",quality=45,full_page=False)
    print("P005_BROWSER_"+name+"="+base64.b64encode(blob).decode(),flush=True)


def run():
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self,*args):
            pass
        def do_GET(self):
            if self.path.startswith("/brand/"):
                self.path="/static/"+self.path[len("/brand/"):]
            return super().do_GET()
    server=ThreadingHTTPServer(
        ("127.0.0.1",0),partial(QuietHandler,directory=str(APP)))
    Thread(target=server.serve_forever,daemon=True).start()
    base=f"http://127.0.0.1:{server.server_port}"
    try:
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(headless=True,args=["--no-sandbox"])
            page=browser.new_page(viewport={"width":1100,"height":820})
            errors=[]
            page.on("pageerror",lambda error:errors.append(str(error)))
            page.add_init_script(FAKE_API)
            page.route("**/*",lambda route:route.continue_()
                       if route.request.url.startswith(base+"/") else route.abort())
            page.goto(base+"/index.html",wait_until="domcontentloaded")
            page.evaluate("page('dashboard')")
            page.evaluate("async () => await refreshDashboard()")
            assert page.locator("#robotState").inner_text()=="NÃO CONFIRMADO"
            assert page.locator("#dashScanStatus").inner_text()=="ESTADO NÃO CONFIRMADO"
            assert "não confirmado entre processos" in page.locator("#dashScanReasons").inner_text()
            assert page.locator("#robotButton").is_disabled()
            detail=page.locator("#dashOperationalDetails").inner_text()
            assert "confirmados e abertos (não liquidados)" in detail
            assert "Aprovação administrativa REAL: não" in detail
            assert page.evaluate(
                "contractStageLabel({state:'CONFIRMED'})"
            )=="Contrato confirmado · aberto · não liquidado"
            assert page.evaluate(
                "contractStageLabel({state:'SETTLED'})"
            )=="Contrato liquidado"
            capture(page,"UNKNOWN_DESKTOP")
            page.locator("#dashOperationalDetails").scroll_into_view_if_needed()
            capture(page,"DETAILS_DESKTOP")
            assert page.evaluate(
                "() => document.getElementById('ordersBody').textContent.includes('Contrato confirmado · aberto · não liquidado')"
            )
            page.set_viewport_size({"width":390,"height":844})
            page.locator("#dashOperationalDetails").scroll_into_view_if_needed()
            capture(page,"DETAILS_MOBILE")

            page.evaluate("window.p005FailStatus=true")
            page.evaluate("async () => await refreshDashboard()")
            assert page.locator("#robotState").inner_text()=="DESATUALIZADO"
            assert page.locator("#robotButton").is_disabled()
            assert "desatualizados" in page.locator("#dashOperationalDetails").inner_text()
            capture(page,"STALE_MOBILE")
            assert not errors, "Unexpected JS page errors: "+str(errors)
            print("P005_BROWSER_OK: unknown/stale status, non-settled CONFIRMED, disabled control, no network",flush=True)
            browser.close()
    finally:
        server.shutdown()


if __name__=="__main__":
    run()
