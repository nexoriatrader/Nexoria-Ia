"""Broker-authoritative expiration selection for NEXORA DEMO options.

No fixed rule such as 'Forex M15 only' is reliable across symbols, option
types, accounts, and trading sessions. The caller sends the supported
candidate durations to Deriv's authenticated proposal API and trades only
durations with successful current proposals and sufficient payout.
"""
SUPPORTED_AUTO_EXPIRIES = (1, 3, 5, 15)


def proposal_durations(market: str, candidate_expiries, *, automatic=True):
    # Market is kept for telemetry/compatibility, never for a blanket cutoff.
    try:
        offered=tuple(dict.fromkeys(int(x) for x in candidate_expiries))
    except (TypeError, ValueError, OverflowError):
        raise ValueError('Expiração configurada inválida') from None
    if not offered or any(x not in SUPPORTED_AUTO_EXPIRIES for x in offered):
        raise ValueError('Expiração configurada inválida')
    return offered
