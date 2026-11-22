"""Verify a downloaded NEXORA 2.0 historical research JSON with stdlib only.

Checksums detect changes but do NOT establish market-source authenticity,
trusted authorship or cryptographic signatures. Never connects to any API,
broker or database and never executes, proposes or authorizes orders.

Usage: python scripts/verify_nexora2_export.py study.json
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import sys
from pathlib import Path

FORMAT = "NEXORA_2_RESEARCH_EXPORT_V1"
SCOPE = "OWN_UNVERIFIED_HISTORICAL_RESEARCH"
SOURCE = "USER_SUPPLIED_UNVERIFIED_OHLC"
VERSION = "nexora2_uploaded_ohlc_v1"
MAX_BYTES = 2_000_000


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON com chave duplicada")
        result[key] = value
    return result


def _nonfinite_constant(value: str):
    raise ValueError("JSON contém valores numéricos não finitos")


def canonical_digest(value) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def verify(bundle: dict) -> dict:
    if (not isinstance(bundle, dict)
            or bundle.get("format") != FORMAT
            or bundle.get("scope") != SCOPE
            or type(bundle.get("study_id")) is not int
            or bundle["study_id"] <= 0):
        raise ValueError("Formato, escopo ou identificador inválido")
    raw = bundle.get("input")
    report = bundle.get("report")
    if (not isinstance(raw, dict) or not isinstance(report, dict)
            or "input" in report
            or report.get("version") != VERSION
            or report.get("source") != SOURCE
            or report.get("execution_allowed") is not False
            or type(report.get("orders_sent")) is not int
            or report["orders_sent"] != 0):
        raise ValueError("Dados ou versão do relatório inválidos")
    evidence = report.get("evidence")
    bars = raw.get("candles")
    if (not isinstance(evidence, dict) or not isinstance(bars, list)
            or evidence.get("market_source_verified") is not False
            or type(evidence.get("input_count")) is not int
            or len(bars) != evidence["input_count"]
            or raw.get("symbol") != report.get("symbol")):
        raise ValueError("Procedência ou contagem inconsistente")
    entry_hash = evidence.get("input_sha256")
    output_hash = evidence.get("result_sha256")
    if any(not isinstance(value, str) or not re.fullmatch("[a-f0-9]{64}", value)
           for value in (entry_hash, output_hash)):
        raise ValueError("Hash inválido ou ausente")
    try:
        input_digest = canonical_digest(raw)
        proof = {key: value for key, value in report.items()
                 if key != "evidence"}
        proof["evidence"] = {
            key: value for key, value in evidence.items()
            if key != "result_sha256"
        }
        result_digest = canonical_digest(proof)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Dados históricos não verificáveis") from None
    if not hmac.compare_digest(entry_hash, input_digest):
        raise ValueError("Checksum dos dados de entrada não confere")
    if not hmac.compare_digest(output_hash, result_digest):
        raise ValueError("Checksum do relatório analítico não confere")
    return {
        "internal_checksums_match": True,
        "market_source_verified": False,
        "input_count": len(bars),
        "source": SOURCE,
        "version": VERSION,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path, help="JSON exportado da NEXORA 2.0")
    args = parser.parse_args(argv)
    try:
        if args.file.stat().st_size > MAX_BYTES:
            raise ValueError("Arquivo acima do limite de 2 MB")
        data = json.loads(
            args.file.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_pairs,
            parse_constant=_nonfinite_constant,
        )
        outcome = verify(data)
    except (OSError, UnicodeError, ValueError, TypeError, OverflowError) as exc:
        print(f"INVALID: {type(exc).__name__}: {str(exc)[:180]}", file=sys.stderr)
        return 1
    print(json.dumps(outcome, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
