"""P0-01: account-scoped risk preferences, with DEMO-only legacy compatibility.

No Deriv API calls, no execution authority, no OAuth token handling.
The existing User/RobotPlan/RobotCycleParams values are legacy DEMO values only.
REAL never inherits those values, even when the user has only one REAL account.
"""
from . import db


def settings_for(session, user, mode, account_id):
    if mode not in ("demo", "real") or not account_id:
        raise ValueError("Conta ou modalidade não selecionada")
    if mode == "demo" and account_id != user.deriv_account_id:
        raise ValueError("Conta DEMO não corresponde ao usuário")
    row = session.get(db.AccountRiskSettings, (user.id, mode, account_id))
    if row is not None:
        return {
            "mode": mode, "account_id": account_id, "configured": True,
            "revision": row.revision, "stake": row.stake,
            "stop_win": row.stop_win, "stop_loss": row.stop_loss,
            "martingale": bool(row.martingale), "gale_count": row.gale_count,
            "multiplier": row.gale_multiplier, "cycles": str(row.cycles_limit),
            "cycle_multiplier": row.cycle_multiplier,
        }
    if mode == "real":
        # Missing REAL settings must never silently copy DEMO amounts or stops.
        return {
            "mode": mode, "account_id": account_id, "configured": False,
            "revision": None, "stake": None, "stop_win": None,
            "stop_loss": None, "martingale": False, "gale_count": 0,
            "multiplier": "2.00", "cycles": "0", "cycle_multiplier": "2.00",
        }
    plan = session.get(db.RobotPlan, user.id)
    cycles = session.get(db.RobotCycleParams, user.id)
    return {
        "mode": "demo", "account_id": user.deriv_account_id,
        "configured": True, "revision": 0, "stake": user.stake,
        "stop_win": user.stop_win, "stop_loss": user.stop_loss,
        "martingale": bool(plan.requested_martingale) if plan else False,
        "gale_count": plan.gale_count if plan else 0,
        "multiplier": plan.requested_multiplier if plan else "2.00",
        "cycles": str(plan.cycles_limit) if plan and plan.cycles_limit is not None else "0",
        "cycle_multiplier": cycles.multiplier if cycles else "2.00",
    }


def save_settings(session, user, mode, account_id, *, stake, stop_win,
                  stop_loss, martingale, gale_count, multiplier, cycles,
                  cycle_multiplier):
    """Store an isolated preference snapshot; caller controls the transaction."""
    if mode not in ("demo", "real") or not account_id:
        raise ValueError("Conta ou modalidade inválida")
    if mode == "demo" and account_id != user.deriv_account_id:
        raise ValueError("Conta DEMO inválida")
    key = (user.id, mode, account_id)
    row = session.get(db.AccountRiskSettings, key)
    if row is None:
        row = db.AccountRiskSettings(
            user_id=user.id, account_mode=mode, account_id=account_id, revision=1
        )
        session.add(row)
    else:
        row.revision += 1
    row.stake = str(stake)
    row.stop_win = str(stop_win)
    row.stop_loss = str(stop_loss)
    row.martingale = bool(martingale)
    row.gale_count = int(gale_count)
    row.gale_multiplier = str(multiplier)
    row.cycles_limit = int(cycles)
    row.cycle_multiplier = str(cycle_multiplier)
    return row
