"""Fail-closed Git history secret audit before making a repo publicly visible.

Read-only. Reports only file paths, line numbers, and signature types; never
prints matched credentials. Checks every *reachable* committed blob in the
locally fetched Git history, not just the latest working tree.

Limits and caveats: requires a full non-shallow clone with all relevant refs.
Cannot prove that no unknown-format, encrypted, indirect, detached or dangling
secrets exist; also review issue attachments, logs, external services and
deleted refs before making a repository public.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from check_repository_security import (
    MAX_TEXT_BYTES, scan_text, suspicious_filename,
)

# Avoid loading arbitrary amounts of historical or binary data in memory.
MAX_OBJECTS = 100_000
MAX_TEXT_AUDIT_BYTES = 80_000_000
SKIP_BINARY_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svgz",
    ".ico", ".pdf", ".woff", ".woff2", ".ttf", ".mp4", ".zip",
})


def _git(root: Path, *args: str, input_data: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=root, input=input_data,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=True, timeout=120,
    ).stdout


def audit_history(root: Path) -> dict:
    if _git(root, "rev-parse", "--is-shallow-repository").strip() != b"false":
        raise RuntimeError("Checkout raso: busque todo o histórico antes da auditoria")
    refs = _git(root, "for-each-ref", "--format=%(refname)").splitlines()
    if not refs:
        raise RuntimeError("Nenhuma referência Git disponível para inspeção")

    findings = set()
    all_paths = _git(root, "log", "--all", "--name-only", "--format=").decode(
        "utf-8", errors="replace"
    ).splitlines()
    for path in all_paths:
        if path.strip() and suspicious_filename(Path(path)):
            findings.add((path, 0, "sensitive_historical_filename"))

    listed = _git(root, "rev-list", "--objects", "--all").splitlines()
    if not listed or len(listed) > MAX_OBJECTS:
        raise RuntimeError("Histórico vazio ou excedeu o limite de objetos auditáveis")

    objects = {}
    for item in listed:
        oid, _, name = item.partition(b" ")
        if len(oid) not in (40, 64):
            raise RuntimeError("Identificador Git inválido no histórico")
        objects[oid] = name.decode("utf-8", errors="replace")
    ids = list(objects)
    metadata = _git(
        root, "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)",
        input_data=b"\n".join(ids) + b"\n",
    ).splitlines()
    if len(metadata) != len(ids):
        raise RuntimeError("Auditoria de histórico incompleta")

    selected = []
    total_bytes = 0
    for line, requested in zip(metadata, ids):
        parts = line.split()
        if len(parts) != 3 or parts[0] != requested:
            raise RuntimeError("Metadados Git inesperados")
        oid, kind, raw_size = parts
        if kind != b"blob":
            continue
        size = int(raw_size)
        path = objects[oid]
        if Path(path).suffix.lower() in SKIP_BINARY_EXTENSIONS:
            continue
        if size > MAX_TEXT_BYTES:
            findings.add((path or oid.decode(), 0, "unscanned_oversized_blob"))
            continue
        total_bytes += size
        if total_bytes > MAX_TEXT_AUDIT_BYTES:
            raise RuntimeError("Histórico de texto grande demais; auditoria incompleta")
        selected.append((oid, path, size))

    if selected:
        payload = _git(
            root, "cat-file", "--batch",
            input_data=b"\n".join(item[0] for item in selected) + b"\n",
        )
        position = 0
        for expected_oid, path, expected_size in selected:
            line_end = payload.find(b"\n", position)
            if line_end < 0:
                raise RuntimeError("Resposta Git truncada")
            header = payload[position:line_end].split()
            if (len(header) != 3 or header[0] != expected_oid
                    or header[1] != b"blob"
                    or int(header[2]) != expected_size):
                raise RuntimeError("Resposta Git inconsistente")
            start = line_end + 1
            end = start + expected_size
            if end >= len(payload) or payload[end:end + 1] != b"\n":
                raise RuntimeError("Blob histórico truncado")
            data = payload[start:end]
            position = end + 1
            if b"\x00" in data[:1024]:
                continue
            for number, category in scan_text(
                data.decode("utf-8", errors="replace")
            ):
                findings.add((path or expected_oid.decode(), number, category))
        if position != len(payload):
            raise RuntimeError("Saída Git contém dados não auditados")

    return {
        "refs_inspected": len(refs),
        "git_objects_examined": len(ids),
        "historical_text_blobs_scanned": len(selected),
        "findings": sorted(findings),
    }


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    try:
        result = audit_history(root)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print("Histórico NÃO aprovado; auditoria incompleta:", type(exc).__name__)
        return 2
    print(
        "Git history audit:", result["refs_inspected"], "refs,",
        result["git_objects_examined"], "objects,",
        result["historical_text_blobs_scanned"], "text blobs",
    )
    if result["findings"]:
        print("Auditoria REPROVADA. Tipos de achados, sem revelar valores:")
        for path, line, kind in result["findings"][:70]:
            print(f"{path}:{line}: {kind}")
        print("Revogue/rotacione credenciais expostas. Não publique este histórico.")
        return 1
    print("Nenhum padrão conhecido encontrado nas referências disponíveis.")
    print("Isto NÃO é certificação de segurança para divulgação pública.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
