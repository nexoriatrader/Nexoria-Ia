"""Read-only historical signal cataloguer for Deriv candle data.

The output is an experimental historical time-slot bias, not a probability,
profit estimate, recommendation, or permission to place an order.
"""
from __future__ import annotations

import asyncio
import math
import re
import time
from datetime import date, datetime, time as clock_time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .market import MarketError, public_request
from .manual_signals import TIMEZONES

SUPPORTED_GRANULARITIES = frozenset((300, 900))
SUPPORTED_LOOKBACK_DAYS = frozenset((30, 60))
MIN_TRAIN_SAMPLES = 20
MIN_WALK_FORWARD_SAMPLES = 8
MIN_TRAIN_RATE = 0.70
MAX_SIGNALS = 5
PAGE_SIZE = 1000
MAX_PAGES = 20
_SYMBOL = re.compile(r"[A-Za-z0-9_]{2,30}\Z")
_history_cache: dict[tuple, tuple[float, list[dict]]] = {}
_history_cache_lock = asyncio.Lock()
_history_fetch_lock = asyncio.Lock()
_CACHE_TTL_SECONDS = 300
_CACHE_MAX_ITEMS = 8


def validate_request(*, symbol: str, granularity_seconds: int,
                     lookback_days: int, target_day: str,
                     timezone_name: str) -> ZoneInfo:
    if not isinstance(symbol, str) or _SYMBOL.fullmatch(symbol) is None:
        raise ValueError("Código de ativo inválido.")
    if type(granularity_seconds) is not int or granularity_seconds not in SUPPORTED_GRANULARITIES:
        raise ValueError("Escolha M5 ou M15.")
    if type(lookback_days) is not int or lookback_days not in SUPPORTED_LOOKBACK_DAYS:
        raise ValueError("O histórico deve ser de 30 ou 60 dias.")
    if target_day not in ("today", "tomorrow"):
        raise ValueError("Escolha hoje ou amanhã.")
    if timezone_name not in TIMEZONES:
        raise ValueError("Fuso horário indisponível.")
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("Fuso horário indisponível neste servidor.") from exc


def _parse_candle(item: dict, granularity_seconds: int) -> dict:
    try:
        epoch = int(item["epoch"])
        open_price = float(item["open"])
        close_price = float(item["close"])
    except (KeyError, ValueError, TypeError, OverflowError) as exc:
        raise MarketError("A Deriv retornou uma vela histórica inválida.") from exc
    if (epoch < 0 or epoch % granularity_seconds != 0
            or not math.isfinite(open_price) or not math.isfinite(close_price)):
        raise MarketError("A Deriv retornou dados de vela inválidos.")
    return {"epoch": epoch, "open": open_price, "close": close_price}


async def fetch_historical_candles(*, symbol: str, granularity_seconds: int,
                                   lookback_days: int,
                                   now_epoch: int | None = None) -> list[dict]:
    """Return a bounded, de-duplicated window of completed Deriv candles."""
    if type(granularity_seconds) is not int or granularity_seconds not in SUPPORTED_GRANULARITIES:
        raise MarketError("Timeframe inválido para o Catalogador.")
    if type(lookback_days) is not int or lookback_days not in SUPPORTED_LOOKBACK_DAYS:
        raise MarketError("Período inválido para o Catalogador.")
    if not isinstance(symbol, str) or _SYMBOL.fullmatch(symbol) is None:
        raise MarketError("Símbolo inválido para o Catalogador.")

    observed_now = int(time.time() if now_epoch is None else now_epoch)
    start_epoch = observed_now - lookback_days * 86400
    cache_key = (symbol, granularity_seconds, lookback_days)
    async with _history_cache_lock:
        cached = _history_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < _CACHE_TTL_SECONDS:
            return [dict(row) for row in cached[1]]
        if cached:
            _history_cache.pop(cache_key, None)

    # One historical catalogue download at a time. Other live research paths
    # retain their own bounded public market client and continue independently.
    async with _history_fetch_lock:
        async with _history_cache_lock:
            cached = _history_cache.get(cache_key)
            if cached and time.monotonic() - cached[0] < _CACHE_TTL_SECONDS:
                return [dict(row) for row in cached[1]]

        all_rows: dict[int, dict] = {}
        end: str | int = "latest"
        prior_oldest: int | None = None
        history_complete = False
        expected_pages = math.ceil(lookback_days * 86400 / granularity_seconds / PAGE_SIZE) + 2
        page_limit = min(MAX_PAGES, expected_pages)
        for page_index in range(page_limit):
            response = await public_request({
                "ticks_history": symbol,
                "style": "candles",
                "granularity": granularity_seconds,
                "count": PAGE_SIZE,
                "end": str(end),
            }, expected="candles", timeout=15.0)
            raw_candles = response.get("candles")
            if not isinstance(raw_candles, list) or not raw_candles:
                history_complete = True
                break
            if any(not isinstance(item, dict) for item in raw_candles):
                raise MarketError("A Deriv retornou uma página de velas inválida.")
            page_rows = [_parse_candle(item, granularity_seconds) for item in raw_candles]
            if not page_rows:
                history_complete = True
                break
            oldest = min(row["epoch"] for row in page_rows)
            newest = max(row["epoch"] for row in page_rows)
            if prior_oldest is not None and oldest >= prior_oldest:
                raise MarketError("A paginação do histórico da Deriv não avançou.")
            prior_oldest = oldest
            for row in page_rows:
                # Candle epoch denotes the interval opening. Exclude any bar
                # whose close is not yet observable at the fixed request time.
                if start_epoch <= row["epoch"] and row["epoch"] + granularity_seconds <= observed_now:
                    all_rows[row["epoch"]] = row
            if oldest <= start_epoch or len(raw_candles) < PAGE_SIZE:
                history_complete = True
                break
            if newest <= 0:
                break
            end = oldest - 1
            if page_index + 1 < page_limit:
                await asyncio.sleep(0.12)
        if not history_complete:
            raise MarketError("O histórico excedeu o limite de páginas da consulta; tente outro período.")

    result = [all_rows[key] for key in sorted(all_rows)]
    async with _history_cache_lock:
        _history_cache[cache_key] = (time.monotonic(), [dict(row) for row in result])
        while len(_history_cache) > _CACHE_MAX_ITEMS:
            oldest_key = next(iter(_history_cache))
            _history_cache.pop(oldest_key, None)
    return result


def _direction(candle: dict) -> str | None:
    if candle["close"] > candle["open"]:
        return "CALL"
    if candle["close"] < candle["open"]:
        return "PUT"
    return None


def _majority(directions: list[str]) -> tuple[str | None, int, float]:
    calls = sum(direction == "CALL" for direction in directions)
    puts = sum(direction == "PUT" for direction in directions)
    total = calls + puts
    if not total or calls == puts:
        return None, max(calls, puts), (max(calls, puts) / total if total else 0.0)
    return ("CALL", calls, calls / total) if calls > puts else ("PUT", puts, puts / total)


def _localize_strict(local_value: datetime, zone: ZoneInfo) -> datetime:
    """Reject nonexistent or ambiguous local times rather than guess DST."""
    first = local_value.replace(tzinfo=zone, fold=0)
    second = local_value.replace(tzinfo=zone, fold=1)
    valid_first = first.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == local_value
    valid_second = second.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == local_value
    if not valid_first and not valid_second:
        raise ValueError("O horário local não existe neste fuso por causa da mudança do relógio.")
    if valid_first and valid_second and first.utcoffset() != second.utcoffset():
        raise ValueError("O horário local é ambíguo neste fuso por causa da mudança do relógio.")
    return first if valid_first else second


def catalogue_time_slots(*, candles: list[dict], granularity_seconds: int,
                         target_day: str, timezone_name: str,
                         now: datetime | None = None,
                         market: str = "derived") -> dict:
    """Find historical same-clock slots and report a separate walk-forward test.

    Directional eligibility and exported direction use only the initial
    training segment. A fixed minimum OOS sample count is required to report
    validation metrics; OOS outcomes never rank or change candidate direction.
    """
    zone = validate_request(
        symbol="TEST", granularity_seconds=granularity_seconds,
        lookback_days=60, target_day=target_day, timezone_name=timezone_name,
    )
    if market not in ("forex", "derived", "commodities", "stocks_indices"):
        raise ValueError("Mercado inválido.")
    now_utc = now or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    now_utc = now_utc.astimezone(timezone.utc)
    local_now = now_utc.astimezone(zone)
    target_date = local_now.date() + timedelta(days=(target_day == "tomorrow"))
    if market == "forex" and target_date.weekday() >= 5:
        return {
            "signals": [], "target_date": target_date.isoformat(),
            "history_candles": len(candles), "history_days": 0,
            "slots_tested": 0, "note": "O mercado Forex fica fechado neste fim de semana. Escolha outro dia.",
        }

    # One observation per local date and clock slot. A tied open/close is kept
    # as a neutral observation so its omission from the directional denominator
    # is explicit and consistent in training and validation.
    by_slot: dict[str, dict[date, str | None]] = {}
    all_dates: set[date] = set()
    for candle in candles:
        epoch = candle.get("epoch")
        if type(epoch) is not int or epoch % granularity_seconds:
            continue
        local = datetime.fromtimestamp(epoch, timezone.utc).astimezone(zone)
        slot = local.strftime("%H:%M")
        local_date = local.date()
        all_dates.add(local_date)
        by_slot.setdefault(slot, {})[local_date] = _direction(candle)

    ordered_dates = sorted(all_dates)
    split = max(MIN_TRAIN_SAMPLES, math.ceil(len(ordered_dates) * 0.75))
    training_dates = ordered_dates[:split]
    validation_dates = ordered_dates[split:]
    if len(training_dates) < MIN_TRAIN_SAMPLES or len(validation_dates) < MIN_WALK_FORWARD_SAMPLES:
        return {
            "signals": [], "target_date": target_date.isoformat(),
            "history_candles": len(candles), "history_days": len(ordered_dates),
            "slots_tested": len(by_slot),
            "note": "Histórico insuficiente para separar treino e validação walk-forward. Tente 60 dias ou outro timeframe.",
        }

    candidates = []
    for slot, daily in by_slot.items():
        train_directions = [daily[d] for d in training_dates
                            if d in daily and daily[d] in ("CALL", "PUT")]
        train_prediction, train_hits, train_rate = _majority(train_directions)
        if (train_prediction is None or len(train_directions) < MIN_TRAIN_SAMPLES
                or train_rate < MIN_TRAIN_RATE):
            continue

        oos_hits = 0
        oos_samples = 0
        for test_date in validation_dates:
            actual = daily.get(test_date)
            if actual not in ("CALL", "PUT"):
                continue
            prior = [daily[d] for d in ordered_dates if d < test_date
                     and daily.get(d) in ("CALL", "PUT")]
            if len(prior) < MIN_TRAIN_SAMPLES:
                continue
            prediction, _, _ = _majority(prior)
            if prediction is None:
                continue
            oos_samples += 1
            oos_hits += prediction == actual
        if oos_samples < MIN_WALK_FORWARD_SAMPLES:
            continue

        historical_directions = [daily[d] for d in ordered_dates
                                 if daily.get(d) in ("CALL", "PUT")]
        # Keep both the candidate and the out-of-sample measurement honest:
        # the held-out validation block cannot change what gets exported.
        future_direction = train_prediction
        if future_direction is None:
            continue
        try:
            local_naive = datetime.combine(target_date, clock_time.fromisoformat(slot))
            local_due = _localize_strict(local_naive, zone)
        except ValueError:
            continue
        due_utc = local_due.astimezone(timezone.utc)
        if target_day == "today" and due_utc <= now_utc:
            continue

        candidates.append({
            "time": slot,
            "direction": future_direction,
            "train_samples": len(train_directions),
            "train_hits": train_hits,
            "train_rate": train_rate,
            "walk_forward_samples": oos_samples,
            "walk_forward_hits": oos_hits,
            "walk_forward_rate": oos_hits / oos_samples,
            "historical_samples": len(historical_directions),
            "scheduled_at_utc": due_utc.isoformat(),
        })

    # The cap is chronological, not an OOS performance ranking. Candidate
    # thresholds were fixed before evaluation and OOS results do not select rows.
    candidates.sort(key=lambda item: item["time"])
    selected = candidates[:MAX_SIGNALS]
    return {
        "signals": selected,
        "target_date": target_date.isoformat(),
        "history_candles": len(candles),
        "history_days": len(ordered_dates),
        "slots_tested": len(by_slot),
        "candidates_before_daily_cap": len(candidates),
        "max_signals": MAX_SIGNALS,
        "note": ("As taxas são observações de candles, não probabilidade calibrada nem retorno de contrato. "
                 "Validação walk-forward usa apenas dados anteriores a cada dia testado; testar muitos horários ainda pode gerar falsos positivos."),
    }
