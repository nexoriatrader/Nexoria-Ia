from pathlib import Path
import shutil
import subprocess

import pytest

HTML = (Path(__file__).resolve().parents[1] / "app" / "index.html").read_text(
    encoding="utf-8"
)


def test_panel_history_prioritizes_live_automatic_order_and_keeps_settled_history():
    if not shutil.which("node"):
        pytest.skip("Node.js is required for the front-end rendering regression test")

    block = HTML.split("function renderPanelHistory(items,mode)", 1)[1].split(
        "\nasync function loadDemoOrders", 1
    )[0]
    function = "function renderPanelHistory(items,mode)" + block
    runtime = r"""
const elements=new Map();
function node(tag='div'){
 return {tag,className:'',dataset:{},textContent:'',children:[],
  replaceChildren(){this.children=[];},append(...items){this.children.push(...items);}};
}
global.document={createElement:node};
function $(id){if(!elements.has(id))elements.set(id,node());return elements.get(id);}
function visibleOrderRows(items){return items;}
function brasilDateTime(value){return value?['27/11/2026','16:15']:['—','—'];}
function money(value){return 'R$ '+value;}
let robotConfiguredMode='demo';

const active={id:'1485',state:'CONFIRMED',symbol:'R_75',direction:'CALL',
 duration_minutes:15,stake:'1.00',pnl:null,outcome:'PENDING',
 created_at:'2026-11-27T19:15:10Z',broker_purchase_at:'2026-11-27T19:15:10Z',
 started_at_brasilia:'2026-11-27T16:15:10-03:00',time_source:'DERIV',
 progression:{source:'autopilot'}};
const settled={id:'1484',state:'SETTLED',symbol:'R_25',direction:'PUT',
 duration_minutes:5,stake:'1.00',pnl:'0.88',outcome:'WIN',
 created_at:'2026-11-27T19:10:00Z',broker_purchase_at:'2026-11-27T19:10:00Z',
 started_at_brasilia:'2026-11-27T16:10:00-03:00',time_source:'DERIV',
 progression:{source:'autopilot'}};

renderPanelHistory([active,settled],'demo');
const latest=$('nxPanelHistoryLatest'),previous=$('nxPanelHistoryPrevious');
if(latest.children.length!==1)throw Error('Expected the active operation in the latest card');
if(latest.children[0].children[0].children[0].textContent!=='R_75')
 throw Error('The latest card did not show the active symbol');
if(latest.children[0].children[1].children[0].textContent!=='Em andamento')
 throw Error('A confirmed order must be labeled as in progress');
if(previous.children.length!==1||previous.children[0].children[0].children[0].textContent!=='R_25')
 throw Error('A settled operation should remain in previous results');

const pending={...active,state:'PENDING',broker_purchase_at:null,started_at_brasilia:null,
 time_source:'NOT_REPORTED'};
renderPanelHistory([pending],'demo');
if(latest.children[0].children[1].children[0].textContent!=='Aguardando confirmação')
 throw Error('A pending order must not be mislabeled as purchased');
if(!latest.children[0].children[0].children[1].textContent.startsWith('Registro ·'))
 throw Error('The local reservation timestamp must not be presented as broker purchase time');

renderPanelHistory([settled],'demo');
if(latest.children[0].children[1].children[0].textContent!=='Ganho')
 throw Error('When no order is active, the most recent settled result should lead');
"""
    result = subprocess.run(
        ["node", "-e", runtime + function],
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_panel_history_copy_includes_active_orders():
    panel = HTML.split('<section class="nx-panel-history"', 1)[1].split(
        "</section>", 1
    )[0]
    assert "Acompanha ordens em andamento e resultados encerrados" in panel
