import pytest
from app.expiry_policy import proposal_durations


@pytest.mark.parametrize('market', ['forex', 'derived', 'commodities', 'stocks_indices'])
def test_all_markets_use_broker_proposal_to_discover_available_expirations(market):
    assert proposal_durations(market, [1, 3, 5, 15]) == (1, 3, 5, 15)


def test_no_blanket_m15_if_user_selects_m5():
    assert proposal_durations('forex', [5], automatic=False) == (5,)
    assert proposal_durations('forex', [5]) == (5,)


def test_unique_order_and_invalid_durations():
    assert proposal_durations('derived', [1, 3, 3, 5]) == (1, 3, 5)
    for values in ([], [2], [1, 2], ['bad']):
        with pytest.raises(ValueError, match='Expiração configurada inválida'):
            proposal_durations('forex', values)
