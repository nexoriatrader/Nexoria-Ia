"""Strategy IDs and account/market eligibility rules."""

BASE_STRATEGIES = frozenset({"pulso_v2", "trend", "rsi"})
FOREX_DEMO_STRATEGIES = frozenset({"fx_pullback_m15", "fx_breakout_m15"})
SUPPORTED_STRATEGIES = BASE_STRATEGIES | FOREX_DEMO_STRATEGIES


def strategy_allowed(strategy: str, market: str, account_mode: str, *,
                     allow_forex_demo: bool = True) -> bool:
    """Keep experimental multi-timeframe Forex strategies out of REAL trading."""
    if strategy in BASE_STRATEGIES:
        return True
    return (
        allow_forex_demo
        and strategy in FOREX_DEMO_STRATEGIES
        and market == "forex"
        and account_mode == "demo"
    )
