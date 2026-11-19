"""Regression for foreground-only read refreshes. No network or broker calls."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_foreground_resync_deduplicates_focus_and_visibility_events():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js unavailable")
    html = (Path(__file__).resolve().parents[1] / "app" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "document.addEventListener('visibilitychange',refreshVisiblePageOnReturn);" in html
    assert "window.addEventListener('focus',refreshVisiblePageOnReturn);" in html
    source = "function refreshVisiblePageOnReturn(){" + html.split(
        "function refreshVisiblePageOnReturn(){", 1
    )[1].split("\ndocument.addEventListener('visibilitychange'", 1)[0]
    assert "start" not in source.lower() and "/api/" not in source

    harness = r"""
    const assert=require('node:assert/strict');
    const document={hidden:true};
    let activePage='results',derivReturnRefreshAt=0;
    let now=10000; Date.now=()=>now;
    const calls=[];
    function loadDemoOrders(){calls.push('orders');return Promise.resolve({items:[]});}
    function refreshDashboard(){calls.push('dashboard');return Promise.resolve({ok:true});}
    function loadDailyResults(){calls.push('daily');return Promise.resolve({});}
    // FUNCTION_UNDER_TEST
    (async()=>{
      refreshVisiblePageOnReturn();
      assert.deepEqual(calls,[],'hidden pages must not refresh');
      document.hidden=false;
      refreshVisiblePageOnReturn();
      await Promise.resolve();
      assert.deepEqual(calls,['orders'],'results page refreshes its history once');
      refreshVisiblePageOnReturn();
      assert.deepEqual(calls,['orders'],'focus+visibility must coalesce');
      activePage='dashboard';
      now+=4000;
      refreshVisiblePageOnReturn();
      await Promise.resolve();
      assert.deepEqual(calls,['orders','dashboard','daily'],'dashboard cards refresh on return');
      document.hidden=true;
      now+=5000;
      refreshVisiblePageOnReturn();
      assert.equal(calls.length,3,'hidden dashboard must not refresh');
      process.stdout.write('FOREGROUND_READONLY_RESYNC_OK\n');
    })().catch(err=>{console.error(err);process.exitCode=1});
    """
    done = subprocess.run(
        [node, "-e", harness.replace("// FUNCTION_UNDER_TEST", source)],
        capture_output=True, text=True, timeout=15,
    )
    assert done.returncode == 0, done.stderr or done.stdout
    assert "FOREGROUND_READONLY_RESYNC_OK" in done.stdout
