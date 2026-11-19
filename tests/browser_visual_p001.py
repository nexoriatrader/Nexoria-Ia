"""P0-01 manual visual inspection aid, Chromium + synthetic account responses.

Run in the isolated branch CI only. No live API or financial action.
Screenshots are emitted as JPEG strings in CI logs for external visual review.
"""
import base64
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from playwright.sync_api import sync_playwright

HTML = Path(__file__).resolve().parents[1] / "app" / "index.html"

SYNTHETIC_FETCH = r"""
(() => {
  const settings = {
    demo: {stake:'2.50',stop_win:'12.00',stop_loss:'8.00',martingale:false,gale_count:0,multiplier:'2.00',cycles:'0',cycle_multiplier:'2.00'},
    CRONE: {stake:'7.00',stop_win:'19.00',stop_loss:'14.00',martingale:false,gale_count:0,multiplier:'2.00',cycles:'0',cycle_multiplier:'2.00'},
    CRTWO: {stake:'3.00',stop_win:'9.00',stop_loss:'6.00',martingale:false,gale_count:0,multiplier:'2.00',cycles:'0',cycle_multiplier:'2.00'}
  };
  const fake = body => ({ok:true, json:async()=>body});
  window.fetch = async (input, options={}) => {
    const url = new URL(String(input), 'https://synthetic.invalid');
    const path = url.pathname;
    if (options.method && options.method !== 'GET') throw Error('Browser visual test forbids POST');
    if (path==='/api/session') return fake({authenticated:false});
    if (path==='/api/public/offer') return fake({trial_hours:24});
    if (path==='/api/accounts/available')
      return fake({real_accounts:[{account_id:'CRONE',currency:'USD'},{account_id:'CRTWO',currency:'USD'}]});
    if (path==='/api/onboarding/config') {
      const account = url.searchParams.get('mode')==='demo' ? 'demo' : url.searchParams.get('account_id');
      if (!settings[account]) throw Error('Unexpected test account: '+account);
      return fake({...settings[account],account_id:account,configured:true});
    }
    if (path==='/api/automation/profile')
      return fake({account_mode:'demo',configured:true,real_account_id:null,market:'derived',
        symbols:['1HZ100V'],min_payout_pct:88,direction:'AUTO',
        strategy:'pulso_v2',indicators:[],preset_mode:'custom'});
    if (path==='/api/automation/catalogue')
      return fake({items:[{symbol:'1HZ100V',name:'Índice sintético demonstrativo'}]});
    throw Error('Unexpected browser visual request: '+path);
  };
})();
"""

def record(page, name):
    # Screenshot real browser rendering (not merely DOM assertions).
    blob = page.screenshot(type="jpeg",quality=48,full_page=False)
    print("P0_VISUAL_" + name + "=" + base64.b64encode(blob).decode(),flush=True)

def state(page, expected, label):
    page.wait_for_function(
      "args => document.getElementById('autoStake').value===args.stake &&"
      "document.getElementById('autoWin').value===args.win &&"
      "document.getElementById('autoLoss').value===args.loss",
      arg=expected, timeout=8000
    )
    actual=page.evaluate("() => ({stake:document.getElementById('autoStake').value,"
      "win:document.getElementById('autoWin').value,loss:document.getElementById('autoLoss').value})")
    print("P0_BROWSER_STATE_" + label + "=" + str(actual),flush=True)

def run():
    # A loopback-only temporary server resolves real project /static assets.
    # This is not a production deploy and cannot contact external services.
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self,*args):
            return
    server = ThreadingHTTPServer(("127.0.0.1", 0),
        partial(QuietHandler, directory=str(HTML.parent)))
    Thread(target=server.serve_forever,daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True,args=["--no-sandbox"])
        context = browser.new_context(viewport={"width":1060,"height":770},
                                      device_scale_factor=1)
        page = context.new_page()
        errors=[]
        page.on("pageerror", lambda err:errors.append(str(err)))
        page.add_init_script(SYNTHETIC_FETCH)
        # Allow only localhost static files; browser fetch is synthetic.
        page.route("**/*",lambda route:route.continue_()
                   if route.request.url.startswith(base+"/") else route.abort())
        page.goto(base+"/index.html",wait_until="domcontentloaded")
        page.evaluate("() => {page('robot-config');initAutomationWizard();setAutomationWizardStep(2)}")
        assert page.locator('input[name="autoMode"][value="demo"]').is_checked()
        page.locator('input[name="autoMode"][value="real"]').check()
        page.locator('#autoRealAccount option[value="CRONE"]').wait_for(state='attached')
        assert page.locator('#autoRealAccountWrapper').is_visible()
        page.locator('#autoRealAccount').select_option('CRONE')
        state(page,{"stake":"7.00","win":"19.00","loss":"14.00"},"REAL_ONE")
        record(page,"REAL_ONE_MODE_DESKTOP")
        page.evaluate("setAutomationWizardStep(6)")
        record(page,"REAL_ONE_DESKTOP")

        page.evaluate("setAutomationWizardStep(2)")
        page.locator('#autoRealAccount').select_option('CRTWO')
        state(page,{"stake":"3.00","win":"9.00","loss":"6.00"},"REAL_TWO")
        page.evaluate("setAutomationWizardStep(6)")
        record(page,"REAL_TWO_DESKTOP")

        page.evaluate("setAutomationWizardStep(2)")
        page.locator('input[name="autoMode"][value="demo"]').check()
        state(page,{"stake":"2.50","win":"12.00","loss":"8.00"},"DEMO")
        assert not page.locator('#autoRealAccountWrapper').is_visible()
        page.evaluate("setAutomationWizardStep(6)")
        record(page,"DEMO_DESKTOP")

        page.set_viewport_size({"width":390,"height":844})
        page.evaluate("setAutomationWizardStep(2)")
        record(page,"DEMO_MOBILE_MODE")
        page.evaluate("setAutomationWizardStep(6)")
        record(page,"DEMO_MOBILE_RISK")
        assert not errors, "Unexpected browser JavaScript errors: "+str(errors)
        print("P0_BROWSER_VISUAL_OK: desktop/mobile, REAL1, REAL2, DEMO; no network or POST",flush=True)
        browser.close()
    server.shutdown()

if __name__=="__main__":
    run()
