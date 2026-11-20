"""Isolated Stage 4 public-research ASGI app; no financial runtime.

This app intentionally does not import app.main, and therefore does not
start the existing DEMO/REAL autopilot or order reconciliation loops.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hmac
import logging
import os
import re
import socket

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import text
from sqlalchemy.engine import make_url

from .. import db
from .homologation_runtime import (
    homologation_enabled, robot_research_view, run_homologation_cycle,
)

log = logging.getLogger("nexora.intelligence20.stage4")
VERSION = "intelligence20_stage4_isolated_live_public_research_v1"
REVISIONS = frozenset((
    "f01_global_fencing_v1",
    "i04_knowledge_base_v1",
    "i20_public_research_coordination_v1",
))
_last_cycle = {"state": "NOT_STARTED"}


def isolated_database_guard() -> None:
    """Independent explicit DB host/name assertions before ANY DB operation."""
    if not homologation_enabled():
        raise RuntimeError("Isolated homologation flags/stage not enabled")
    if os.getenv("NEXORA_INTELLIGENCE_HOMOLOGATION_LIVE_WORKER_ENABLED") != "true":
        raise RuntimeError("Isolated public worker not explicitly enabled")
    if os.getenv("NEXORA_DEMO_BUY_ENABLED", "false").strip().lower() == "true":
        raise RuntimeError("Demo purchases must remain disabled")
    if not db.DB_URL.startswith("postgresql+psycopg://"):
        raise RuntimeError("Isolated PostgreSQL required")
    parsed = make_url(db.DB_URL)
    name = os.getenv("NEXORA_HOMOLOGATION_DATABASE_NAME", "")
    host = os.getenv("NEXORA_HOMOLOGATION_DATABASE_HOST", "")
    if (not name or "homolog" not in name.lower() or not host
            or parsed.database != name or parsed.host != host):
        raise RuntimeError("Database identity mismatch: refuse production risk")
    secret = os.getenv("NEXORA_HOMOLOGATION_ACCESS_TOKEN", "")
    if not (32 <= len(secret) <= 256 and secret.isascii()):
        raise RuntimeError("Missing isolated API token (32-256 ASCII characters)")


def verify_formal_schema() -> None:
    """Startup performs only SELECTs. Never DDL or migration repair."""
    db.init_db()
    with db.engine.connect() as connection:
        installed = set(connection.execute(text(
            "SELECT revision FROM nexora_schema_revisions "
            "WHERE revision IN "
            "('f01_global_fencing_v1', 'i04_knowledge_base_v1', "
            "'i20_public_research_coordination_v1')"
        )).scalars())
        if installed != REVISIONS:
            raise RuntimeError("F01/I04/public coordination migrations required")
        cap = connection.execute(text(
            "SELECT dynamic_limit FROM public_research_capacity WHERE id=1"
        )).scalar_one_or_none()
        if type(cap) is not int or not 1 <= cap <= 8:
            raise RuntimeError("Invalid persisted public research capacity")


def _auth(secret: str | None) -> None:
    if not homologation_enabled():
        raise HTTPException(503, "Read-only homologation disabled")
    expected = os.getenv("NEXORA_HOMOLOGATION_ACCESS_TOKEN", "")
    if not expected or secret is None or not hmac.compare_digest(secret, expected):
        raise HTTPException(401, "Research access key required")


def _closed_candle_identity(now: datetime) -> tuple[str, dict]:
    last_closed = (int(now.timestamp()) // 60 - 1) * 60
    return (
        "stage4_public_closed_m1_" + str(last_closed),
        {"source_version": "i03_public_v1", "closed_epoch": last_closed,
         "timeframe_seconds": 60},
    )


def _worker_id() -> str:
    suffix = re.sub(r"[^a-zA-Z0-9_-]", "_", socket.gethostname())[:34]
    return "homo_" + (suffix or "worker")


async def run_live_loop(stop: asyncio.Event, *, interval: int = 90) -> None:
    """Bounded live research polling, existing queue+fencing+Deriv public client."""
    global _last_cycle
    if type(interval) is not int or not 60 <= interval <= 300:
        raise ValueError("Research interval must be between 60 and 300 seconds")
    while not stop.is_set():
        at = datetime.now(timezone.utc)
        key, provenance = _closed_candle_identity(at)
        try:
            if not homologation_enabled():
                log.error("stage4_public_research_safety_gate_closed")
                _last_cycle = {"state": "GATE_CLOSED"}
                break
            result = await run_homologation_cycle(
                task_key=key, market="public", symbol="SHARED",
                source=provenance, worker_id=_worker_id(), now=at,
            )
            _last_cycle = {
                "state": result["worker_state"],
                "at_epoch": int(at.timestamp()),
                "task_created": result["enqueue"]["created"],
                "execution_allowed": False,
            }
            log.info("stage4_public_research_cycle state=%s created=%s",
                     result["worker_state"], result["enqueue"]["created"])
        except asyncio.CancelledError:
            raise
        except Exception:
            _last_cycle = {
                "state": "RESEARCH_UNAVAILABLE", "at_epoch": int(at.timestamp()),
                "execution_allowed": False,
            }
            log.exception("stage4_public_research_cycle_failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


@asynccontextmanager
async def lifespan(_: FastAPI):
    isolated_database_guard()
    verify_formal_schema()
    try:
        interval = int(os.getenv("NEXORA_HOMOLOGATION_CYCLE_INTERVAL_SECONDS", "90"))
    except ValueError as exc:
        raise RuntimeError("Invalid stage4 interval") from exc
    if not 60 <= interval <= 300:
        raise RuntimeError("Stage4 interval must be 60-300s")
    stop = asyncio.Event()
    task = asyncio.create_task(run_live_loop(stop, interval=interval),
                               name="isolated-research-worker")
    try:
        yield
    finally:
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


app = FastAPI(title="NEXORA Intelligence 2.0 isolated public research",
              version=VERSION, lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers.update({
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy":
            "default-src 'none'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self'; "
            "frame-ancestors 'none'; form-action 'none'; base-uri 'none'",
    })
    return response


@app.get("/healthz")
async def healthz():
    # Liveness alone must never be reported as proof of real Deriv feed.
    return {"state": "PROCESS_READY", "version": VERSION,
            "execution_allowed": False}


@app.get("/api/homologation/robot/research")
async def research(view: str = "autonomous",
                   x_nexora_homologation_key: str | None = Header(default=None)):
    _auth(x_nexora_homologation_key)
    try:
        return await asyncio.to_thread(robot_research_view, view=view)
    except ValueError:
        raise HTTPException(422, "Invalid research display view") from None
    except PermissionError:
        raise HTTPException(503, "Homologation disabled") from None
    except Exception:
        log.exception("stage4_shared_snapshot_unavailable")
        raise HTTPException(503, "Shared research unavailable") from None


@app.get("/api/homologation/status")
async def status(x_nexora_homologation_key: str | None = Header(default=None)):
    _auth(x_nexora_homologation_key)
    try:
        report = await asyncio.to_thread(robot_research_view, view="autonomous")
    except Exception:
        log.exception("stage4_research_status_unavailable")
        raise HTTPException(503, "Research status unavailable") from None
    return {
        "version": VERSION,
        "cycle": dict(_last_cycle),
        "snapshot_state": report["state"],
        "snapshot_digest": report["snapshot_digest"],
        "visible_items": len(report["items"]),
        "execution_allowed": False,
        "orders_sent": 0,
        "real_feed_confirmed": False,
    }


_HTML = """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NEXORA 2.0 | Homologação isolada</title>
<style>body{font:16px system-ui,sans-serif;background:#0b192b;color:#e9f4f7;
max-width:960px;margin:0 auto;padding:28px}p,small{color:#b9d0dd}
section{background:#152b41;border-radius:12px;padding:18px;margin:16px 0}
input,select,button{font:inherit;padding:10px;border-radius:6px}
button{background:#11b7a6;border:0;color:#05242b}
pre{white-space:pre-wrap;overflow-wrap:anywhere}
</style></head><body>
<h1>NEXORA Intelligence 2.0 — Homologação</h1>
<p>Dados públicos de pesquisa; sem sinais, ordens ou execução financeira.</p>
<section><label>Chave de homologação
<input type="password" id="key" autocomplete="off"></label>
<label>Visão <select id="view"><option>autonomous</option>
<option>signals</option><option>advanced</option></select></label>
<button type="button" id="refresh">Consultar</button>
<p><small>A chave fica apenas na memória desta página.</small></p></section>
<section><h2>Estado</h2><pre id="status">Não consultado</pre></section>
<section><h2>Pesquisa</h2><pre id="research">Não consultada</pre></section>
<script>
async function read(path){
 const resp=await fetch(path,{cache:'no-store',headers:{
  'X-NEXORA-HOMOLOGATION-KEY':document.getElementById('key').value}});
 if(!resp.ok)throw Error('HTTP '+resp.status);
 return resp.json();
}
async function refresh(){
 const status=document.getElementById('status');
 const research=document.getElementById('research');
 try{
  const view=encodeURIComponent(document.getElementById('view').value);
  const values=await Promise.all([read('/api/homologation/status'),
    read('/api/homologation/robot/research?view='+view)]);
  status.textContent=JSON.stringify(values[0],null,2);
  research.textContent=JSON.stringify(values[1],null,2);
 }catch(error){status.textContent='Falha: '+error.message;research.textContent='';}
}
document.getElementById('refresh').addEventListener('click',refresh);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
async def viewer():
    return _HTML
