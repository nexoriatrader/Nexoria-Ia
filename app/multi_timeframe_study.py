"""Exploratory M1/M3/M5/M15 historical backtests; NEVER sends Deriv orders.

Use original Deriv M1 close at signal bar completion and close of minute
at expiry as a fill approximation. Training is chosen before holdout.
Return at +88% is an illustrative constant, not historical broker payout.
"""
from __future__ import annotations
import hashlib
import math
from datetime import datetime, timezone
from .m1_history import _read_existing, SYMBOL, MINUTE
from .smc_study import make_smc_signals

TIMEFRAMES = (1, 3, 5, 15)
EXPIRIES = (1, 3, 5, 15)
STRATEGIES = (
    "ema_trend", "rsi_reversion", "bollinger_reentry",
    "channel_breakout", "momentum_3", "random_control"
)
PAYOUT = .88
BREAK_EVEN = 1 / (1 + PAYOUT)


def aggregate(bars, minutes):
    """Group from exact UTC boundaries; discard incomplete aggregates."""
    n = minutes * MINUTE
    groups = {}
    for row in bars:
        epoch = row[0]
        groups.setdefault(epoch - epoch % n, []).append(row)
    out = []
    for epoch in sorted(groups):
        rows = groups[epoch]
        if len(rows) != minutes or any(rows[i][0] != epoch + i * MINUTE for i in range(minutes)):
            continue
        out.append((epoch, rows[0][1], max(r[2] for r in rows),
                    min(r[3] for r in rows), rows[-1][4]))
    return out


def make_signals(bars):
    """All indicators use current and prior completed bars only."""
    signals = {key: [0] * len(bars) for key in STRATEGIES}
    if len(bars) < 60:
        return signals
    closes = [r[4] for r in bars]
    # Run EMA recursively; do not rescan large windows for every candle.
    fast, slow = closes[0], closes[0]
    fast_last, slow_last = fast, slow
    ups = downs = 0.
    for i in range(1, len(bars)):
        c, prev = closes[i], closes[i - 1]
        fast_last, slow_last = fast, slow
        fast += (2 / 21) * (c - fast)
        slow += (2 / 51) * (c - slow)
        change = c - prev
        ups += max(change, 0)
        downs += max(-change, 0)
        if i < 60:
            continue
        # RSI 14 (simple window variant), deliberately no tuneable thresholds.
        changes = [closes[k] - closes[k - 1] for k in range(i - 13, i + 1)]
        gain = sum(x for x in changes if x > 0)
        loss = -sum(x for x in changes if x < 0)
        rsi = 100 * gain / (gain + loss) if gain + loss > 0 else 50
        if fast > slow and c > fast and c > bars[i][1] and fast >= fast_last:
            signals["ema_trend"][i] = 1
        elif fast < slow and c < fast and c < bars[i][1] and fast <= fast_last:
            signals["ema_trend"][i] = -1
        if rsi < 30 and c > bars[i][1]:
            signals["rsi_reversion"][i] = 1
        elif rsi > 70 and c < bars[i][1]:
            signals["rsi_reversion"][i] = -1
        prev20 = closes[i - 20:i]
        mid = sum(prev20) / 20
        dev = math.sqrt(sum((x - mid)**2 for x in prev20) / 20)
        if dev:
            low, high = mid - 2 * dev, mid + 2 * dev
            if closes[i - 1] < low and c > low:
                signals["bollinger_reentry"][i] = 1
            elif closes[i - 1] > high and c < high:
                signals["bollinger_reentry"][i] = -1
        prior = bars[i - 20:i]
        if c > max(b[2] for b in prior):
            signals["channel_breakout"][i] = 1
        elif c < min(b[3] for b in prior):
            signals["channel_breakout"][i] = -1
        if c > closes[i-3] and closes[i-1] > closes[i-2] > closes[i-3]:
            signals["momentum_3"][i] = 1
        elif c < closes[i-3] and closes[i-1] < closes[i-2] < closes[i-3]:
            signals["momentum_3"][i] = -1
        signals["random_control"][i] = 1 if hashlib.sha256(str(bars[i][0]).encode()).digest()[0] & 1 else -1
    return signals


def score(bars, signal, m1_close, expiry, start_epoch, end_epoch):
    wins = losses = draws = 0
    next_entry = -1
    first = last = None
    for i, bar in enumerate(bars):
        epoch = bar[0]
        tf_minutes = (bars[1][0] - bars[0][0]) // MINUTE if len(bars) > 1 else 1
        entry_epoch = epoch + tf_minutes * MINUTE - MINUTE
        exit_epoch = entry_epoch + expiry * MINUTE
        if entry_epoch < start_epoch or exit_epoch >= end_epoch or entry_epoch < next_entry:
            continue
        direction = signal[i]
        if not direction:
            continue
        # Both closing prices must be in verified M1 dataset, no future leakage.
        entry = m1_close.get(entry_epoch)
        exit_price = m1_close.get(exit_epoch)
        if entry is None or exit_price is None:
            continue
        delta = exit_price - entry
        if delta == 0:
            draws += 1
        elif delta * direction > 0:
            wins += 1
        else:
            losses += 1
        next_entry = exit_epoch + MINUTE
        if first is None:
            first = entry_epoch
        last = entry_epoch
    total = wins + losses + draws
    n = wins + losses
    rate = wins / n if n else None
    lower = None
    if n:
        z = 1.96
        den = 1 + z*z/n
        lower = (rate + z*z/(2*n) - z*math.sqrt(rate*(1-rate)/n + z*z/(4*n*n))) / den
    return {
        "signals": total, "wins": wins, "losses": losses, "draws": draws,
        "win_rate": round(rate, 5) if rate is not None else None,
        "wilson_95_lower": round(lower, 5) if lower is not None else None,
        "pnl_per_1_usd_at_88pct": round(PAYOUT * wins - losses, 2),
        "first_signal_epoch": first, "last_signal_epoch": last
    }


def analyze_m1(bars):
    if not bars or any(b[0] - a[0] != MINUTE for a,b in zip(bars, bars[1:])) or len(bars) < 43000:
        raise ValueError("M1 continuity or coverage insufficient for research")
    m1_close = {bar[0]: bar[4] for bar in bars}
    begin = bars[0][0]
    end = bars[-1][0] + MINUTE
    split = begin + ((end - begin) * 7 // 10 // MINUTE) * MINUTE
    rows = []
    for tf in TIMEFRAMES:
        grouped = aggregate(bars, tf)
        directions = make_signals(grouped)
        for expiry in EXPIRIES:
            for strategy in STRATEGIES:
                sig = directions[strategy]
                train = score(grouped, sig, m1_close, expiry, begin, split)
                holdout = score(grouped, sig, m1_close, expiry, split, end)
                rows.append({"timeframe":f"M{tf}", "expiry_minutes":expiry,
                             "strategy":strategy, "training":train, "holdout":holdout})
    # User-requested SMC candidate: same splits and expiry assumptions.
    smc_definitions = []
    for tf in (1, 5):
        sparse, definition = make_smc_signals(bars, tf)
        smc_definitions.append(definition)
        grouped = aggregate(bars, tf)
        trigger_directions = [sparse.get(bar[0] + (tf-1)*MINUTE, 0) for bar in grouped]
        for expiry in EXPIRIES:
            train = score(grouped, trigger_directions, m1_close, expiry, begin, split)
            holdout = score(grouped, trigger_directions, m1_close, expiry, split, end)
            rows.append({"timeframe":f"M{tf}", "expiry_minutes":expiry,
                         "strategy":"smc_sweep_choch_fvg_50",
                         "training":train, "holdout":holdout})
    # Rank by training profitability, then disclose holdout only after selection.
    eligible = [x for x in rows if x["training"]["signals"] >= 100]
    eligible.sort(key=lambda x: x["training"]["pnl_per_1_usd_at_88pct"] /
                  max(1, x["training"]["signals"]), reverse=True)
    selected = eligible[0] if eligible else None
    for row in rows:
        v = row["holdout"]
        row["passed_exploratory_threshold"] = bool(
            v["signals"] >= 100 and v["wilson_95_lower"] is not None and
            v["wilson_95_lower"] > BREAK_EVEN and v["pnl_per_1_usd_at_88pct"] > 0)
    return {
        "state": "RESEARCH_ONLY", "symbol": SYMBOL,
        "period_from": datetime.fromtimestamp(begin, timezone.utc).isoformat(),
        "period_to": datetime.fromtimestamp(end, timezone.utc).isoformat(),
        "m1_candles": len(bars), "split": "70% / 30% chronological, strict expiry boundaries",
        "return_per_win_assumed": PAYOUT, "break_even_rate": round(BREAK_EVEN, 5),
        "models_tested": len(rows),
        "smc_definitions": smc_definitions,
        "training_selected": {"timeframe":selected["timeframe"],
                              "expiry_minutes":selected["expiry_minutes"],
                              "strategy":selected["strategy"],
                              "training":selected["training"],
                              "holdout":selected["holdout"]} if selected else None,
        "results": rows,
        "orders_sent": 0, "execution_enabled": False,
        "payout_actual_verified": False,
        "warning": ("Exploratory directional M1 closing-price simulations only, not Deriv settled contracts. "
                    "The 88% net payout is hypothetical, and prices at actual order execution differ. "
                    "Multi-model selection risks overfitting; positive holdouts require independent forward validation. "
                    "No Martingale or cycles. No live trading authorized.")
    }


def run_multitimeframe_backtest():
    data = _read_existing()
    bars = [data[k] for k in sorted(data)]
    return analyze_m1(bars)
