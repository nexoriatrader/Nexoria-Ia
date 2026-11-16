"""Expiry preflight policy for automatic DEMO proposals.

Forex AUTO is intentionally M15-only until alternate Forex expiries have
been explicitly supported in the NEXORA strategy. Other markets retain
multi-expiry discovery and the broker decides actual eligibility.
No unsupported duration is ever silently substituted.
"""

SUPPORTED_AUTO_EXPIRIES = (1, 3, 5, 15)


def proposal_durations(market: str, candidate_expiries, *, automatic=True):
    offered=tuple(dict.fromkeys(int(x) for x in candidate_expiries))
    if not offered or any(x not in SUPPORTED_AUTO_EXPIRIES for x in offered):
        raise ValueError("Expiração configurada inválida")
    if automatic and market == 'forex':
        return (15,) if 15 in offered else ()
    return offered
