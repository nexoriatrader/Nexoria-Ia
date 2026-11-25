"""Strategy IDs and account/market eligibility rules."""

BASE_STRATEGIES = frozenset({"pulso_v2", "trend", "rsi"})
FOREX_MTF_STRATEGIES = frozenset({"fx_pullback_m15", "fx_breakout_m15"})
SUPPORTED_STRATEGIES = BASE_STRATEGIES | FOREX_MTF_STRATEGIES


def strategy_allowed(strategy: str, market: str, account_mode: str) -> bool:
    """Allow the three core strategies in DEMO/REAL and MTF strategies on Forex only."""
    if account_mode not in ("demo", "real"):
        return False
    if strategy in BASE_STRATEGIES:
        return True
    return strategy in FOREX_MTF_STRATEGIES and market == "forex"
