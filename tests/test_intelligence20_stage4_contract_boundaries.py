"""Static contract guards: no broker, market feed, orders or external calls."""
from __future__ import annotations
import ast
from pathlib import Path
from app.intelligence2 import stage4_observability as monitor

ROOT=Path(__file__).resolve().parents[1] / "app" / "intelligence2"

def fn(filename,name):
    tree=ast.parse((ROOT / filename).read_text(encoding="utf-8"))
    return next(n for n in tree.body
                if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))
                and n.name==name)

def calls(node):
    return {n.func.attr if isinstance(n.func,ast.Attribute) else n.func.id
            for n in ast.walk(node) if isinstance(n,ast.Call)
            and isinstance(n.func,(ast.Attribute,ast.Name))}

def test_source_market_contracts_are_not_generic_provider_injections():
    i01=fn("market_data.py","read_closed_window")
    assert {"shared_candle_request","strict_closed_bars"}<=calls(i01)
    assert "provider" not in {x.arg for x in i01.args.args+i01.args.kwonlyargs}
    i02=fn("market_scanner.py","scan_market_opportunities")
    assert "market_instruments" in calls(i02)
    assert "discover_contracts" in calls(fn("market_scanner.py","_inspect"))
    assert "provider" not in {x.arg for x in i02.args.args+i02.args.kwonlyargs}

def test_offline_analysis_and_read_only_provider_contracts_exist():
    for path,name in (
        ("context_engine.py","analyze_context"),
        ("timeframe_engine.py","align_timeframes"),
        ("learning_engine.py","evaluate_public_regimes"),
        ("research_signal_engine.py","assess_research_report"),
        ("research_orchestrator.py","consolidate_research_report"),
    ):
        assert fn(path,name)
    a=fn("i04_research_adapter.py","enrich_research_with_i04")
    b=fn("i05_research_adapter.py","enrich_research_with_i05")
    assert "provider" in {x.arg for x in a.args.kwonlyargs}
    assert {"registry","knowledge"} <= {x.arg for x in b.args.kwonlyargs}

def test_homologation_module_status_not_inferred_from_static_contracts():
    items=monitor._module_catalog(
        latest_synthetic_at=None,demo_fresh=False,run_count_value=0)
    tracked=[i for i in items if i["id"] in monitor.OFFLINE_CONTRACT_CAPABILITIES]
    assert len(tracked)==7
    assert {i["id"] for i in tracked}==set(monitor.OFFLINE_CONTRACT_CAPABILITIES)
    for item in tracked:
        assert item["status"] in ("NOT_CONNECTED","NOT_AVAILABLE")
        assert item["running_in_service"] is False
        assert item["initialized_in_service"] is False
        assert item["last_update"] is None
        assert item["offline_functions_not_executed_here"] is True
        assert item["contract_capabilities"]["missing_dependencies"]
        assert item["contract_capabilities"]["data_provider_injected"] is False
    assert next(i for i in tracked if i["id"]=="I04")["contract_capabilities"]["read_only_knowledge_provider_injected"]
    assert next(i for i in tracked if i["id"]=="I05")["contract_capabilities"]["read_only_registry_provider_injected"]

def test_isolated_stage4_demo_never_imports_broker_market_entrypoints():
    source=(ROOT / "stage4_general_homologation.py").read_text(encoding="utf-8")
    imports=[n.module for n in ast.walk(ast.parse(source))
             if isinstance(n,ast.ImportFrom)]
    assert not any(name in {"app.market","app.asset_scanner",
        "app.intelligence2.market_data","app.intelligence2.market_scanner",
        "app.intelligence2.learning_engine"} for name in imports)
