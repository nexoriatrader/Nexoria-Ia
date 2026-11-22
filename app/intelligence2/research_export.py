"""Lossless, owner-scoped research export, with offline checksum verification.

Checksums detect differences in exported data. They do NOT authenticate market
origin, prove trusted authorship, constitute signatures or guarantee results.
All content originates in the user's previously validated historical study.
No brokerage interface, orders, execution or new database schema.
"""
from __future__ import annotations

import hashlib
import hmac
import json

from .historical_research import SOURCE, VERSION, result_digest

EXPORT_FORMAT = "NEXORA_2_RESEARCH_EXPORT_V1"
EXPORT_SCOPE = "OWN_UNVERIFIED_HISTORICAL_RESEARCH"


def export_report(report: dict, study_id: int) -> dict:
    """Preserve full context/indicator evidence, not a reduced UI summary."""
    if type(study_id) is not int or study_id <= 0:
        raise ValueError("Identificador da pesquisa inválido")
    if (not isinstance(report, dict)
            or report.get("version") != VERSION
            or report.get("source") != SOURCE
            or report.get("orders_sent") != 0
            or report.get("execution_allowed") is not False):
        raise ValueError("Relatório histórico incompatível")
    # Eliminate duplicated input while retaining every versioned result field.
    # The authenticated store validates both hashes before this is called.
    return {
        "format": EXPORT_FORMAT,
        "scope": EXPORT_SCOPE,
        "study_id": study_id,
        "input": report["input"],
        "report": {key: value for key, value in report.items() if key != "input"},
    }


def verify_export_checksums(bundle: dict) -> dict:
    """Check exported input/output integrity without a DB or network.

    This is **not** a signature; an attacker able to replace the bundle and
    its hashes can forge matching hashes. Market provenance stays unverified.
    """
    if not isinstance(bundle, dict) or bundle.get("format") != EXPORT_FORMAT:
        raise ValueError("Formato de exportação inválido")
    if (bundle.get("scope") != EXPORT_SCOPE
            or type(bundle.get("study_id")) is not int
            or bundle["study_id"] <= 0):
        raise ValueError("Identificador ou escopo inválido")
    inp = bundle.get("input")
    result = bundle.get("report")
    if not isinstance(inp, dict) or not isinstance(result, dict):
        raise ValueError("Entrada ou relatório ausente")
    if ("input" in result or result.get("version") != VERSION
            or result.get("source") != SOURCE
            or result.get("orders_sent") != 0
            or result.get("execution_allowed") is not False):
        raise ValueError("Metadados do relatório inválidos")
    evidence = result.get("evidence")
    if not isinstance(evidence, dict):
        raise ValueError("Evidência histórica ausente")
    input_hash = evidence.get("input_sha256")
    output_hash = evidence.get("result_sha256")
    if any(not isinstance(value, str) or len(value) != 64
           for value in (input_hash, output_hash)):
        raise ValueError("Hashes não reconhecidos")
    try:
        normalized = json.dumps(inp, sort_keys=True,
                                separators=(",", ":"), allow_nan=False).encode()
        actual_input = hashlib.sha256(normalized).hexdigest()
        # Include original input when checking consistency of the full result.
        actual_output = result_digest({"input": inp, **result})
    except (TypeError, ValueError, KeyError, OverflowError):
        raise ValueError("Dados exportados não verificáveis") from None
    if not hmac.compare_digest(actual_input, input_hash):
        raise ValueError("Checksum da entrada histórica não confere")
    if not hmac.compare_digest(actual_output, output_hash):
        raise ValueError("Checksum do resultado histórico não confere")
    if (evidence.get("market_source_verified") is not False
            or inp.get("symbol") != result.get("symbol")
            or evidence.get("input_count") != len(inp.get("candles", []))):
        raise ValueError("Procedência ou contagem de entrada inconsistentes")
    return {
        "internal_checksums_match": True,
        "market_source_verified": False,
        "source": SOURCE,
        "version": VERSION,
        "input_count": evidence["input_count"],
    }
