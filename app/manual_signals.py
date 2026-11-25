"""Parsing and safe validation for user-supplied, scheduled Deriv signals.

This module is deliberately read-only: preview never opens a trading channel or
places an order. Execution and persistent queue state are owned by main.py and
the existing DEMO/REAL ledgers.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .automation_markets import GROUPS

MAX_MANUAL_SIGNALS_PER_SESSION = 100
MAX_INPUT_LINES = 500
MAX_INPUT_CHARS = 20_000
ENTRY_WINDOW_SECONDS = 60
ALLOWED_DURATIONS = (1, 3, 5, 15)
DEFAULT_TIMEZONE = "America/Sao_Paulo"
TIMEZONES = {
    "America/Sao_Paulo": "Brasília (UTC−03:00) · padrão",
    "America/Noronha": "Fernando de Noronha",
    "America/Manaus": "Manaus",
    "America/Rio_Branco": "Rio Branco",
    "UTC": "UTC",
    "America/New_York": "Nova York",
    "America/Chicago": "Chicago",
    "America/Denver": "Denver",
    "America/Los_Angeles": "Los Angeles",
    "Europe/London": "Londres",
    "Europe/Lisbon": "Lisboa",
    "Europe/Madrid": "Madri",
}
MARKET_LABELS = dict(GROUPS)
_SIGNAL = re.compile(
    r"^\s*M(1|3|5|15)\s+([A-Za-z0-9_]{2,30})\s+(\d{2}):(\d{2})\s+(CALL|PUT)\s*$",
    re.IGNORECASE,
)


class ManualSignalInputError(ValueError):
    """The request itself is invalid, rather than one individual signal line."""


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _offset_label(value: datetime) -> str:
    offset = value.utcoffset()
    if offset is None:
        return "UTC"
    total = int(offset.total_seconds() // 60)
    sign = "+" if total >= 0 else "−"
    hours, minutes = divmod(abs(total), 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def _localize_strict(local_value: datetime, zone: ZoneInfo) -> datetime:
    """Reject daylight-saving gaps and folds instead of guessing an instant."""
    first = local_value.replace(tzinfo=zone, fold=0)
    second = local_value.replace(tzinfo=zone, fold=1)
    roundtrip_first = first.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
    roundtrip_second = second.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
    valid_first = roundtrip_first == local_value
    valid_second = roundtrip_second == local_value
    if not valid_first and not valid_second:
        raise ManualSignalInputError("Horário inexistente neste fuso devido à mudança de relógio.")
    if valid_first and valid_second and first.utcoffset() != second.utcoffset():
        raise ManualSignalInputError("Horário ambíguo neste fuso devido à mudança de relógio.")
    return first if valid_first else second


def _catalogue_maps(catalogue: list[dict]) -> tuple[dict[str, dict], dict[str, dict]]:
    exact: dict[str, dict] = {}
    aliases: dict[str, dict] = {}
    for item in catalogue:
        symbol = item.get("symbol")
        group = item.get("group")
        if not isinstance(symbol, str) or group not in MARKET_LABELS:
            continue
        code = symbol.upper()
        safe = {"symbol": symbol, "group": group,
                "name": str(item.get("name") or symbol)[:100]}
        exact[code] = safe
        aliases.setdefault(code, safe)
        # Deriv prefixes its OTC Forex/commodity codes with frx. Accept the
        # familiar pair name only when that exact broker-listed item exists.
        if code.startswith("FRX") and len(code) > 3:
            aliases.setdefault(code[3:], safe)
    return exact, aliases


def _line_error(line_no: int, raw: str, reason: str, code: str = "invalid") -> dict:
    return {"line_number": line_no, "raw_line": raw[:255], "code": code,
            "reason": reason}


def validate_signal_list(*, text: str, market: str, day: str,
                         timezone_name: str, catalogue: list[dict],
                         now: datetime | None = None) -> dict:
    """Validate every nonblank line and return valid items plus per-line issues.

    A schedule's minute is the only entry window. The executor must not start a
    buy after that minute; there is no grace period or catch-up behavior.
    """
    if market not in MARKET_LABELS:
        raise ManualSignalInputError("Escolha um mercado válido para esta lista.")
    if day not in ("today", "tomorrow"):
        raise ManualSignalInputError("Escolha se a lista é para hoje ou amanhã.")
    if timezone_name not in TIMEZONES:
        raise ManualSignalInputError("Escolha um fuso horário disponível.")
    if not isinstance(text, str) or len(text) > MAX_INPUT_CHARS:
        raise ManualSignalInputError("A lista excede o tamanho permitido.")
    lines = text.splitlines()
    if len(lines) > MAX_INPUT_LINES:
        raise ManualSignalInputError(f"A lista pode ter no máximo {MAX_INPUT_LINES} linhas.")
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ManualSignalInputError("Fuso horário indisponível neste servidor.") from exc

    now_utc = _utc(now or datetime.now(timezone.utc))
    local_today = now_utc.astimezone(zone).date()
    target_date = local_today + timedelta(days=(day == "tomorrow"))
    exact, aliases = _catalogue_maps(catalogue)
    valid: list[dict] = []
    issues: list[dict] = []
    seen: set[tuple] = set()

    for line_no, raw in enumerate(lines, start=1):
        if not raw.strip():
            continue
        match = _SIGNAL.fullmatch(raw)
        if not match:
            issues.append(_line_error(
                line_no, raw,
                "Formato inválido. Use: M15 EURUSD 19:00 CALL (duração, ativo, horário e direção).",
                "malformed"))
            continue
        minutes = int(match.group(1))
        typed_symbol = match.group(2).upper()
        hour, minute = int(match.group(3)), int(match.group(4))
        direction = match.group(5).upper()
        if hour > 23 or minute > 59:
            issues.append(_line_error(line_no, raw, "Horário inválido; use HH:MM entre 00:00 e 23:59.", "malformed"))
            continue
        item = exact.get(typed_symbol) or aliases.get(typed_symbol)
        if item is None:
            issues.append(_line_error(
                line_no, raw,
                f"Ativo {typed_symbol} não encontrado no catálogo público atual da Deriv; confira o código.",
                "unknown_asset"))
            continue
        if item["group"] != market:
            other = MARKET_LABELS.get(item["group"], item["group"])
            chosen = MARKET_LABELS[market]
            issues.append(_line_error(
                line_no, raw,
                f"Ativo {item['symbol']} pertence a {other}, não a {chosen}; removido desta programação. Os outros sinais válidos permanecem.",
                "outside_market"))
            continue
        local_naive = datetime.combine(target_date, time(hour, minute))
        try:
            local_due = _localize_strict(local_naive, zone)
        except ManualSignalInputError as exc:
            issues.append(_line_error(line_no, raw, str(exc), "ambiguous_time"))
            continue
        due_utc = local_due.astimezone(timezone.utc)
        window_end = due_utc + timedelta(seconds=ENTRY_WINDOW_SECONDS)
        if now_utc >= window_end:
            issues.append(_line_error(
                line_no, raw,
                "O minuto indicado já passou; este sinal não será executado nem recuperado.",
                "past"))
            continue
        dedupe_key = (due_utc, item["symbol"].upper(), minutes, direction)
        if dedupe_key in seen:
            issues.append(_line_error(
                line_no, raw,
                "Sinal duplicado nesta lista; removido para evitar uma ordem repetida.",
                "duplicate"))
            continue
        seen.add(dedupe_key)
        valid.append({
            "line_number": line_no,
            "raw_line": raw[:255],
            "market": market,
            "timezone_name": timezone_name,
            "local_date": target_date.isoformat(),
            "local_time": f"{hour:02d}:{minute:02d}",
            "utc_offset": _offset_label(local_due),
            "scheduled_at": due_utc,
            "symbol": item["symbol"],
            "display_name": item["name"],
            "direction": direction,
            "duration_minutes": minutes,
        })

    return {
        "market": market,
        "market_label": MARKET_LABELS[market],
        "day": day,
        "date": target_date.isoformat(),
        "timezone_name": timezone_name,
        "timezone_label": TIMEZONES[timezone_name],
        "valid": valid,
        "issues": issues,
        "entry_window_seconds": ENTRY_WINDOW_SECONDS,
        "max_per_session": MAX_MANUAL_SIGNALS_PER_SESSION,
    }
