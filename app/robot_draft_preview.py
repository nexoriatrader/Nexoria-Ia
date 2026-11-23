"""Read-only educational NEXORA wizard preview.

No broker access, trading orders, market feeds, account balance, price
simulation, or order-prediction capability. A preview reflects an actual
validated saved draft and explicitly marks unsupported execution flows.
"""
from decimal import Decimal


def preview_saved_wizard(*, mode: str, draft: dict) -> dict:
    """Produce a deterministic, bounded explanation of saved planning settings."""
    if mode not in ("intelligent", "manual"):
        raise ValueError("Invalid wizard mode")
    if draft.get("recovery") not in ("none", "cycles", "martingale"):
        raise ValueError("Invalid recovery")
    recovery = draft["recovery"]
    if mode == "intelligent" and recovery == "martingale":
        raise ValueError("Martingale is not an intelligent setting")
    # Inputs are produced by validate_robot_wizard_draft, not by caller JSON.
    stake, stop_win, stop_loss = (Decimal(draft[key]) for key in
                                  ("stake", "stop_win", "stop_loss"))
    if not all(x.is_finite() for x in (stake, stop_win, stop_loss)):
        raise ValueError("Nonfinite draft")
    notes = []
    if stop_loss == 0:
        notes.append("Stop Loss está sem limite definido neste rascunho.")
    if stop_win == 0:
        notes.append("Stop Win está sem limite definido neste rascunho.")
    if recovery != "none":
        notes.append(
            "Progressão é apenas ilustrativa. Multiplicadores podem elevar "
            "perdas; não há garantia de recuperação."
        )
    notes.append(
        "O assistente não está integrado ao executor e não autoriza "
        "nenhuma operação na corretora."
    )
    stages = [
        {"key": "preferences", "status": "SAVED_DRAFT",
         "label": "Preferências verificadas e salvas"},
        {"key": "research" if mode == "intelligent" else "diary",
         "status": "READ_ONLY_SEPARATE" if mode == "intelligent"
                   else "SCHEDULE_ONLY",
         "label": ("Intelligence 2.0: pesquisa isolada em homologação"
                   if mode == "intelligent" else
                   "Agenda: somente registros de estudo")},
        {"key": "execution", "status": "NOT_CONNECTED",
         "label": "Executor não conectado a este rascunho"},
    ]
    return {
        "mode": mode,
        "kind": "SAVED_DRAFT_EDUCATIONAL_PREVIEW",
        "validation": "VALID_SAVED_DRAFT",
        "stages": stages,
        "notices": notes,
        "read_only": True,
        "applied_to_executor": False,
        "broker_contacted": False,
        "market_data_used": False,
        "orders_sent": 0,
        "results_simulated": False,
        "predicted_outcome": None,
    }
