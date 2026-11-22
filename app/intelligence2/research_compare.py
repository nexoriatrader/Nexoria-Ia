"""Descriptive, non-predictive comparison of two saved OHLC studies.

Studies must be integrity-checked and scoped to the same authenticated user
before invoking this pure function. No asset ranking, signals or broker calls.
"""
from .historical_research import SOURCE


def _profile(report):
    stats = report["statistics"]
    n = stats["changes_observed"]
    if type(n) is not int or n <= 0:
        raise ValueError("Relatório histórico sem períodos suficientes")
    return {
        "symbol": report["symbol"],
        "period_from_epoch": report["period_from_epoch"],
        "period_to_epoch": report["period_to_epoch"],
        "input_sha256": report["evidence"]["input_sha256"],
        "result_sha256": report["evidence"]["result_sha256"],
        "candles": stats["periods"],
        "observations": n,
        "up_fraction": round(stats["up_intervals"] / n, 6),
        "down_fraction": round(stats["down_intervals"] / n, 6),
        "unchanged_fraction": round(stats["unchanged_intervals"] / n, 6),
        "mean_change": stats["mean_change"],
        "median_change": stats["median_change"],
        "realized_volatility": stats["realized_volatility"],
    }


def compare_saved_reports(left, right):
    """Expose descriptive differences, not investment comparisons."""
    if left["source"] != SOURCE or right["source"] != SOURCE:
        raise ValueError("Pesquisas devem ter origem histórica equivalente")
    a, b = _profile(left), _profile(right)
    return {
        "comparison_kind": "CROSS_ASSET" if a["symbol"] != b["symbol"] else "SAME_ASSET",
        "source": SOURCE,
        "method": "DESCRIPTIVE_HISTORICAL_COMPARISON_V1",
        "left": a, "right": b,
        "delta_right_minus_left": {
            field: round(b[field] - a[field], 9) for field in (
                "up_fraction", "down_fraction", "unchanged_fraction",
                "mean_change", "median_change", "realized_volatility",
            )
        },
        "matching_windows": (
            a["period_from_epoch"] == b["period_from_epoch"]
            and a["period_to_epoch"] == b["period_to_epoch"]
        ),
        "execution_allowed": False,
        "orders_sent": 0,
        "disclaimer": (
            "Estudos históricos enviados por usuários; procedência não verificada. "
            "As janelas e os tamanhos das amostras podem diferir. "
            "Não constitui ranking, previsão ou resultado financeiro."
        ),
    }
