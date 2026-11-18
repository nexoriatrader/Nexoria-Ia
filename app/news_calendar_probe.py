"""Owner-only read-only probe of Deriv's legacy economic_calendar.

This API is retired and MUST NOT be used as an active news filter until
availability, timestamps, completeness and impact semantics are validated.
No user tokens or trade commands are sent.
"""
import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone

import websockets

LEGACY_CALENDAR_URL = "wss://ws.derivws.com/websockets/v3"
_last_probe_at = 0.0
_cached_result = None
_probe_lock = asyncio.Lock()


def _validated_events(payload):
    events = payload.get("economic_calendar")
    if not isinstance(events, list):
        raise ValueError("Calendar response missing event list")
    normalized = []
    for raw in events[:200]:
        if not isinstance(raw, dict):
            continue
        try:
            epoch = int(raw.get("date", raw.get("time", 0)))
        except (ValueError, TypeError):
            continue
        if not 1_600_000_000 <= epoch <= 4_200_000_000:
            continue
        currency = str(raw.get("currency", "")).upper()
        if len(currency) != 3 or not currency.isalpha():
            continue
        normalized.append({
            "time_utc": datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(),
            "currency": currency,
            "title": str(raw.get("event", raw.get("name", "")))[:160],
            "impact_raw": str(raw.get("impact", ""))[:40],
        })
    return normalized


async def probe_calendar():
    """For diagnostics only: never supplies trade authorization or risk decisions."""
    global _cached_result, _last_probe_at
    async with _probe_lock:
        now = time.monotonic()
        if _cached_result is not None and now - _last_probe_at < 300:
            return dict(_cached_result)
        result = {"provider": "deriv_legacy_economic_calendar",
                  "news_filter_enabled": False, "safe_for_trading": False,
                  "status": "unavailable", "events": [], "reason": ""}
        try:
            app_id = os.getenv("DERIV_LEGACY_APP_ID", "").strip()
            # No automatic dependency on the new-style Deriv OAuth app ID.
            url = LEGACY_CALENDAR_URL + ("?app_id=" + app_id if app_id.isalnum() else "")
            start = datetime.now(timezone.utc)
            payload = {"economic_calendar": 1,
                       "start_date": int(start.timestamp()),
                       "end_date": int((start + timedelta(days=7)).timestamp()),
                       "req_id": 7648}
            async with asyncio.timeout(6):
                async with websockets.connect(url, open_timeout=4, close_timeout=1,
                                              max_size=500_000) as ws:
                    await ws.send(json.dumps(payload))
                    response = json.loads(await ws.recv())
            if not isinstance(response, dict):
                raise ValueError("Invalid calendar response")
            if response.get("error"):
                result["reason"] = "Deriv rejected the legacy calendar request"
            else:
                events = _validated_events(response)
                result["status"] = "responding"
                result["events"] = events[:30]
                result["event_count_sampled"] = len(events)
                result["reason"] = "Unverified legacy source: calendar filter remains disabled"
        except (OSError, asyncio.TimeoutError, ValueError, TypeError,
                websockets.exceptions.WebSocketException) as exc:
            result["reason"] = "Legacy Deriv calendar unavailable (" + type(exc).__name__ + ")"
        _cached_result, _last_probe_at = dict(result), time.monotonic()
        return result
