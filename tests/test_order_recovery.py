import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.demo import APP_ID
from app.order_recovery import (
    RecoverySnapshotError,
    _validate_portfolio_contracts,
    _validate_statement_transactions,
    account_recovery_snapshot,
    classify_unidentified_order,
)


def make_order(created_at=None, stake='1.00'):
    return SimpleNamespace(
        id=9,
        symbol='1HZ100V',
        direction='CALL',
        stake=stake,
        buy_price=None,
        created_at=created_at or datetime(2026, 11, 25, 17, 0, tzinfo=timezone.utc),
    )


def test_portfolio_only_candidate_is_not_linked_without_statement():
    order = make_order()
    epoch = int(order.created_at.timestamp())
    contract = {
        'contract_id': 12345,
        'underlying_symbol': '1HZ100V',
        'contract_type': 'CALL',
        'buy_price': '1.00',
        'purchase_time': epoch,
    }
    result = classify_unidentified_order(order, [contract], [], APP_ID)
    assert result[0] == 'AMBIGUOUS'
    assert result[1] is None


def test_portfolio_candidate_is_linked_when_statement_confirms_same_id():
    order = make_order()
    epoch = int(order.created_at.timestamp())
    contract = {
        'contract_id': 12345,
        'underlying_symbol': '1HZ100V',
        'contract_type': 'CALL',
        'buy_price': '1.00',
        'purchase_time': epoch,
    }
    transaction = {
        'action_type': 'buy', 'app_id': APP_ID, 'amount': '-1.00',
        'shortcode': 'CALL_1HZ100V', 'contract_id': 12345,
        'transaction_time': epoch,
    }
    result = classify_unidentified_order(order, [contract], [transaction], APP_ID)
    assert result[0:2] == ('MATCH', '12345')


def test_ambiguous_portfolio_contracts_never_choose_one():
    order = make_order()
    epoch = int(order.created_at.timestamp())
    contract = {
        'contract_id': 12345,
        'underlying_symbol': '1HZ100V',
        'contract_type': 'CALL',
        'buy_price': '1.00',
        'purchase_time': epoch,
    }
    result = classify_unidentified_order(order, [contract, dict(contract, contract_id=12346)], [], APP_ID)
    assert result[0] == 'AMBIGUOUS'
    assert result[1] is None


def test_unique_statement_purchase_returns_linkable_evidence():
    order = make_order()
    epoch = int(order.created_at.timestamp())
    transaction = {
        'action_type': 'buy', 'app_id': APP_ID, 'amount': '-1.00',
        'shortcode': 'CALL_1HZ100V', 'contract_id': 12345,
        'transaction_time': epoch,
    }
    status, contract_id, evidence = classify_unidentified_order(order, [], [transaction], APP_ID)
    assert (status, contract_id) == ('MATCH', '12345')
    assert evidence['underlying_symbol'] == order.symbol
    assert evidence['contract_type'] == order.direction
    assert evidence['buy_price'] == '1.00'
    assert evidence['purchase_time'] == epoch


def test_valid_purchase_below_reserved_stake_is_not_classified_absent():
    order = make_order(stake='2.00')
    epoch = int(order.created_at.timestamp())
    contract = {
        'contract_id': 12345,
        'underlying_symbol': '1HZ100V',
        'contract_type': 'CALL',
        'buy_price': '1.95',
        'purchase_time': epoch,
    }
    result = classify_unidentified_order(order, [contract], [], APP_ID)
    assert result[0] == 'AMBIGUOUS'

    transaction = {
        'action_type': 'buy', 'app_id': APP_ID, 'amount': '-1.95',
        'shortcode': 'CALL_1HZ100V', 'contract_id': 12345,
        'transaction_time': epoch,
    }
    statement_result = classify_unidentified_order(order, [], [transaction], APP_ID)
    assert statement_result[0:2] == ('MATCH', '12345')


def test_irregular_purchase_above_stake_is_ambiguous_not_absent():
    order = make_order(stake='2.00')
    epoch = int(order.created_at.timestamp())
    contract = {
        'contract_id': 12345,
        'underlying_symbol': '1HZ100V',
        'contract_type': 'CALL',
        'buy_price': '2.10',
        'purchase_time': epoch,
    }
    portfolio_result = classify_unidentified_order(order, [contract], [], APP_ID)
    assert portfolio_result[0] == 'AMBIGUOUS'

    transaction = {
        'action_type': 'buy', 'app_id': APP_ID, 'amount': '-2.10',
        'shortcode': 'CALL_1HZ100V', 'contract_id': 12345,
        'transaction_time': epoch,
    }
    statement_result = classify_unidentified_order(order, [], [transaction], APP_ID)
    assert statement_result[0] == 'AMBIGUOUS'


def test_portfolio_match_does_not_mask_conflicting_statement_contracts():
    order = make_order()
    epoch = int(order.created_at.timestamp())
    contract = {
        'contract_id': 12345,
        'underlying_symbol': '1HZ100V',
        'contract_type': 'CALL',
        'buy_price': '1.00',
        'purchase_time': epoch,
    }
    transactions = [
        {'action_type': 'buy', 'app_id': APP_ID, 'amount': '-1.00',
         'shortcode': 'CALL_1HZ100V', 'contract_id': '12345', 'transaction_time': epoch},
        {'action_type': 'buy', 'app_id': APP_ID, 'amount': '-1.00',
         'shortcode': 'CALL_1HZ100V', 'contract_id': '12346', 'transaction_time': epoch},
    ]
    result = classify_unidentified_order(order, [contract], transactions, APP_ID)
    assert result[0] == 'AMBIGUOUS'
    assert result[1] is None


def test_incompletely_identified_nearby_purchase_fails_closed():
    order = make_order()
    transaction = {
        'action_type': 'buy', 'amount': '-1.00',
        'transaction_time': int(order.created_at.timestamp()),
    }
    result = classify_unidentified_order(order, [], [transaction], APP_ID)
    assert result[0] == 'AMBIGUOUS'
    assert result[1] is None


def test_only_complete_empty_sources_can_classify_absent():
    order = make_order()
    assert classify_unidentified_order(order, [], [], APP_ID) == (
        'ABSENT', None, 'portfolio e extrato completos sem compra compatível'
    )


def test_malformed_broker_records_fail_closed_before_absence_decision():
    with pytest.raises(RecoverySnapshotError):
        _validate_portfolio_contracts([{'contract_id': '1'}])
    with pytest.raises(RecoverySnapshotError):
        _validate_statement_transactions([{'action_type': 'buy', 'amount': '-1'}])
    _validate_portfolio_contracts([])
    _validate_statement_transactions([])


def test_snapshot_reads_portfolio_and_paginated_statement_without_trading(monkeypatch):
    from app import demo, order_recovery

    class Socket:
        def __init__(self):
            self.sent = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def send(self, message):
            self.sent.append(json.loads(message))

    socket = Socket()
    monkeypatch.setattr(demo, 'get_demo_ws_url', AsyncMock(return_value='wss://test.invalid'))
    monkeypatch.setattr(demo, '_response_for', AsyncMock(side_effect=[
        {'msg_type': 'portfolio', 'req_id': 9810, 'portfolio': {'contracts': []}},
        {'msg_type': 'statement', 'req_id': 9820,
         'statement': {'count': 0, 'transactions': []},
         'echo_req': {'statement': 1, 'date_from': 1760000000, 'date_to': 1760000100,
                      'description': 1, 'limit': 999, 'offset': 0}},
    ]))
    monkeypatch.setattr(order_recovery.websockets, 'connect', lambda *args, **kwargs: socket)

    snapshot = asyncio.run(account_recovery_snapshot('demo', 'token', 'VDEMO', 1760000000, 1760000100))

    assert snapshot == {'contracts': [], 'transactions': [], 'complete': True}
    assert socket.sent[0] == {'portfolio': 1, 'req_id': 9810}
    assert socket.sent[1]['statement'] == 1
    assert socket.sent[1]['offset'] == 0
    assert 'buy' not in socket.sent[0] and 'buy' not in socket.sent[1]


def test_snapshot_continues_after_short_page_until_empty_page(monkeypatch):
    from app import demo, order_recovery

    class Socket:
        def __init__(self):
            self.sent = []
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def send(self, message): self.sent.append(json.loads(message))

    socket = Socket()
    monkeypatch.setattr(demo, 'get_demo_ws_url', AsyncMock(return_value='wss://test.invalid'))
    first = {'msg_type': 'statement', 'req_id': 9820,
        'statement': {'count': 1, 'transactions': [{'action_type': 'sell', 'amount': '1',
            'transaction_time': 1760000001}]},
        'echo_req': {'statement': 1, 'date_from': 1760000000, 'date_to': 1760000100,
            'description': 1, 'limit': 999, 'offset': 0}}
    second = {'msg_type': 'statement', 'req_id': 9821,
        'statement': {'count': 0, 'transactions': []},
        'echo_req': {'statement': 1, 'date_from': 1760000000, 'date_to': 1760000100,
            'description': 1, 'limit': 999, 'offset': 1}}
    monkeypatch.setattr(demo, '_response_for', AsyncMock(side_effect=[
        {'msg_type': 'portfolio', 'req_id': 9810, 'portfolio': {'contracts': []}},
        first, second,
    ]))
    monkeypatch.setattr(order_recovery.websockets, 'connect', lambda *args, **kwargs: socket)

    snapshot = asyncio.run(account_recovery_snapshot('demo', 'token', 'VDEMO', 1760000000, 1760000100))

    assert snapshot['complete'] is True and len(snapshot['transactions']) == 1
    assert [item['offset'] for item in socket.sent if 'statement' in item] == [0, 1]


def test_statement_page_count_and_echo_must_match_exact_request():
    from app.order_recovery import _validated_statement_page
    message = {'msg_type': 'statement', 'req_id': 9820,
        'statement': {'count': 0, 'transactions': []},
        'echo_req': {'statement': 1, 'date_from': 1760000000, 'date_to': 1760000100,
            'description': 1, 'limit': 999, 'offset': 1}}
    with pytest.raises(RecoverySnapshotError):
        _validated_statement_page(message, req_id=9820, offset=0, page_size=999,
                                  date_from=1760000000, date_to=1760000100)
    message['echo_req']['offset'] = 0
    message['statement']['count'] = 1
    with pytest.raises(RecoverySnapshotError):
        _validated_statement_page(message, req_id=9820, offset=0, page_size=999,
                                  date_from=1760000000, date_to=1760000100)
