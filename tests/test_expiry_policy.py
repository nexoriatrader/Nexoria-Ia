from app.expiry_policy import proposal_durations


def test_forex_auto_probes_only_m15():
    assert proposal_durations('forex', [1, 3, 5, 15]) == (15,)
    assert proposal_durations('forex', [1, 5]) == ()


def test_other_markets_keep_configured_candidates():
    assert proposal_durations('derived', [1, 3, 5, 15]) == (1, 3, 5, 15)
    assert proposal_durations('commodities', [1, 3, 5, 15]) == (1, 3, 5, 15)


def test_manual_selection_is_not_automatically_rewritten():
    assert proposal_durations('forex', [5], automatic=False) == (5,)
