"""Broker-derived market groups for the NEXORA automation configurator.

The list is obtained from Deriv's public active_symbols endpoint. Instrument
classification uses broker metadata rather than a fixed fictitious watchlist.
This catalogue does NOT buy contracts or assert account-specific eligibility.
"""
from __future__ import annotations

import re
import time
import asyncio
import logging

from .market import MarketError, public_request

log=logging.getLogger('nexora.catalogue')

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
    if re.search(r"\bforex\b", combined) or (re.search(r"\bcurrency\b", combined) and "synthetic" not in combined):
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


def _broker_market_group(asset):
    """Preserve known groups and also cover new, broker-declared markets.

    The source must be the public Deriv market field, not a guessed ticker
    prefix or an invented tradeable category. Unknown/unlabelled assets stay
    explicitly unclassified for the research scanner to reject safely.
    """
    known = _classify(asset)
    if known is not None:
        return known
    raw = asset.get("market")
    if isinstance(raw, str):
        slug = re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")
        if 1 <= len(slug) <= 32 and slug not in ("unknown", "none", "null"):
            return "deriv_" + slug
    return "unclassified"


def _broker_boolean(value):
    """Three-valued broker metadata: missing is not automatically open."""
    if value in (True, 1, "1", "true", "True"):
        return True
    if value in (False, 0, "0", "false", "False"):
        return False
    return None


async def market_instruments(force=False):
    now = time.monotonic()
    if not force and _catalog_cache["items"] and now - _catalog_cache["at"] < 300:
        return list(_catalog_cache["items"])
    lock_started=time.monotonic()
    async with _catalog_lock:
        lock_wait_ms=(time.monotonic()-lock_started)*1000
        now = time.monotonic()
        if not force and _catalog_cache["items"] and now - _catalog_cache["at"] < 300:
            return list(_catalog_cache["items"])
        broker_started=time.monotonic()
        try:
            raw = await public_request({"active_symbols": "full"}, expected="active_symbols", timeout=15)
        except Exception:
            log.warning('public catalogue refresh failed lock_wait_ms=%.1f broker_ms=%.1f',
                        lock_wait_ms,(time.monotonic()-broker_started)*1000)
            raise
        broker_ms=(time.monotonic()-broker_started)*1000
        if broker_ms>=1000 or lock_wait_ms>=1000:
            log.warning('public catalogue refresh slow lock_wait_ms=%.1f broker_ms=%.1f',
                        lock_wait_ms,broker_ms)
        else:
            log.info('public catalogue refresh lock_wait_ms=%.1f broker_ms=%.1f',
                     lock_wait_ms,broker_ms)
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
            group = _broker_market_group(asset)
            label = str(asset.get("underlying_symbol_name") or asset.get("display_name") or symbol).strip()
            result[symbol] = {
                "symbol": symbol, "name": label[:100], "group": group,
                # Public catalogue hints only. No account, price, funds or
                # concrete contract eligibility can be inferred from them.
                "catalogue_open": _broker_boolean(asset.get("exchange_is_open")),
                "catalogue_suspended": _broker_boolean(asset.get("is_trading_suspended")),
            }
        items = sorted(result.values(), key=lambda item: (item["group"], item["name"].casefold(), item["symbol"]))
        if not items:
            raise MarketError("Deriv não retornou instrumentos classificados")
        _catalog_cache.update(at=now, items=items)
        return list(items)


# Curated presentation only: names come from Deriv active_symbols.
# These are recognizable instrument preferences, NOT profitability rankings.
FEATURED_SYMBOLS = {
    "forex": ("frxEURUSD","frxGBPUSD","frxUSDJPY","frxAUDUSD","frxUSDCAD"),
    "derived": ("R_10","R_25","R_50","R_75","R_100",
                "1HZ10V","1HZ25V","1HZ50V","1HZ75V","1HZ100V"),
    "stocks_indices": ("OTC_SP500","OTC_NASDAQ","OTC_DJI","OTC_FTSE","OTC_NIKKEI"),
    "commodities": ("frxXAUUSD","frxXAGUSD","frxXTIUSD","frxXBRUSD","frxXPTUSD"),
}


async def featured_instruments_by_group(group):
    """Up to five real broker-listed options per market; never fabricate assets."""
    items=await instruments_by_group(group)
    priorities={symbol.upper():position for position,symbol
                in enumerate(FEATURED_SYMBOLS[group])}
    return sorted(items,key=lambda x:(
        priorities.get(x['symbol'].upper(),9999),
        x['name'].casefold(),x['symbol']))[:5]


async def instruments_by_group(group):
    if group not in GROUPS:
        raise ValueError("Mercado inválido")
    return [item for item in await market_instruments() if item["group"] == group]
