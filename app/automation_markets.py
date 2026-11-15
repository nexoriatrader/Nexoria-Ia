"""Broker-derived market groups for the NEXORA automation configurator.

The list is obtained from Deriv's public active_symbols endpoint. Instrument
classification uses broker metadata rather than a fixed fictitious watchlist.
This catalogue does NOT buy contracts or assert account-specific eligibility.
"""
from __future__ import annotations

import re
import time
import asyncio

from .market import MarketError, public_request

GROUPS = {
    "forex": "Forex",
    "derived": "Derivados",
    "stocks_indices": "Ações e índices",
    "commodities": "Matérias-primas",
}
_catalog_cache = {"at": 0.0, "items": []}
_catalog_lock = asyncio.Lock()


def _classify(asset):
    market = str(asset.get("market") or asset.get("market_display_name") or "").lower().replace("_"," ")
    submarket = str(asset.get("submarket") or asset.get("submarket_display_name") or "").lower().replace("_"," ")
    symbol = str(asset.get("symbol") or asset.get("underlying_symbol") or "")
    label = str(asset.get("underlying_symbol_name") or asset.get("display_name") or symbol)
    combined = " ".join((market, submarket))
    code = symbol.upper()

    # Deriv's metadata is authoritative when present. The order avoids
    # confusing stock indices (e.g. NASDAQ 100) with synthetic indices.
    if any(x in combined for x in ("commodity", "commodities", "metal", "energy", "agricultur")):
        return "commodities"
    if any(x in combined for x in ("stock index", "stock indices", "stocks", "equities", "indices")) and not any(
        x in combined for x in ("synthetic", "derived", "volatility", "random", "jump", "step", "boom", "crash")
    ):
        return "stocks_indices"
    if any(x in combined for x in ("synthetic", "derived", "volatility", "random", "jump", "step", "boom", "crash",
                                   "range break", "basket", "dex", "drift", "tactical")):
        return "derived"
    if "forex" in combined or ("currency" in combined and "synthetic" not in combined):
        return "forex"

    # The fields of brief/full market datasets may differ across API versions.
    # Use code-based fallbacks only for unambiguous known instrument families.
    if re.fullmatch(r"(?:1HZ\d+V|R_\d+|BOOM\d+|CRASH\d+|JD\d+|STP\d+|RDBEAR|RDBULL)", code):
        return "derived"
    if re.match(r"^FRX(?:XAU|XAG|XPT|XPD)", code):
        return "commodities"
    if re.match(r"^FRX[A-Z]{6}$", code):
        return "forex"
    if any(x in label.lower() for x in ("gold/usd", "silver/usd", "crude oil", "natural gas")):
        return "commodities"
    return None


async def market_instruments(force=False):
    now = time.monotonic()
    if not force and _catalog_cache["items"] and now - _catalog_cache["at"] < 300:
        return list(_catalog_cache["items"])
    async with _catalog_lock:
        now = time.monotonic()
        if not force and _catalog_cache["items"] and now - _catalog_cache["at"] < 300:
            return list(_catalog_cache["items"])
        raw = await public_request({"active_symbols": "full"}, expected="active_symbols", timeout=15)
        source = raw.get("active_symbols")
        if not isinstance(source, list):
            raise MarketError("Catálogo de instrumentos inválido")
        result = {}
        for asset in source:
            if not isinstance(asset, dict):
                continue
            symbol = asset.get("symbol") or asset.get("underlying_symbol")
            if not isinstance(symbol, str) or not re.fullmatch(r"[A-Za-z0-9_]{2,30}", symbol):
                continue
            group = _classify(asset)
            if group is None:
                continue
            label = str(asset.get("underlying_symbol_name") or asset.get("display_name") or symbol).strip()
            result[symbol] = {"symbol": symbol, "name": label[:100], "group": group}
        items = sorted(result.values(), key=lambda item: (item["group"], item["name"].casefold(), item["symbol"]))
        if not items:
            raise MarketError("Deriv não retornou instrumentos classificados")
        _catalog_cache.update(at=now, items=items)
        return list(items)


async def instruments_by_group(group):
    if group not in GROUPS:
        raise ValueError("Mercado inválido")
    return [item for item in await market_instruments() if item["group"] == group]
