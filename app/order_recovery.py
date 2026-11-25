"""Read-only broker evidence for safely reconciling uncertain order reservations."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import websockets


class RecoverySnapshotError(Exception):
    """The broker snapshot was unavailable or incomplete; keep every lock."""


def _as_utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _decimal(value):
    if isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _valid_epoch(value):
    if isinstance(value, bool):
        return False
    number = _decimal(value)
    return number is not None and Decimal('946684800') <= number <= Decimal('4102444800')


def _validate_portfolio_contracts(contracts):
    if not isinstance(contracts, list):
        raise RecoverySnapshotError('Portfolio Deriv incompleto')
    for contract in contracts:
        if not isinstance(contract, dict):
            raise RecoverySnapshotError('Portfolio Deriv contém registro inválido')
        cid = str(contract.get('contract_id') or '')
        price = _decimal(contract.get('buy_price'))
        stamp = contract.get('purchase_time')
        if stamp is None:
            stamp = contract.get('date_start')
        if (not cid.isdigit()
            or not isinstance(contract.get('underlying_symbol'), str)
            or not contract.get('underlying_symbol')
            or not isinstance(contract.get('contract_type'), str)
            or not contract.get('contract_type')
            or price is None or price <= 0
            or not _valid_epoch(stamp)):
            raise RecoverySnapshotError('Portfolio Deriv contém dados incompletos')


def _validate_statement_transactions(transactions):
    if not isinstance(transactions, list):
        raise RecoverySnapshotError('Extrato Deriv incompleto')
    for transaction in transactions:
        if (not isinstance(transaction, dict)
            or not isinstance(transaction.get('action_type'), str)
            or not transaction.get('action_type')
            or _decimal(transaction.get('amount')) is None
            or not _valid_epoch(transaction.get('transaction_time'))):
            raise RecoverySnapshotError('Extrato Deriv contém dados incompletos')


def _validated_statement_page(message, *, req_id, offset, page_size, date_from, date_to):
    if (not isinstance(message, dict) or message.get('msg_type') != 'statement'
        or message.get('req_id') != req_id):
        raise RecoverySnapshotError('Resposta de extrato Deriv não corresponde à consulta')
    statement = message.get('statement')
    transactions = statement.get('transactions') if isinstance(statement, dict) else None
    count = statement.get('count') if isinstance(statement, dict) else None
    if (not isinstance(transactions, list) or isinstance(count, bool)
        or not isinstance(count, (int, float)) or int(count) != count
        or count != len(transactions)):
        raise RecoverySnapshotError('Contagem do extrato Deriv incompleta ou inconsistente')
    echo = message.get('echo_req')
    expected = {'statement': 1, 'date_from': int(date_from), 'date_to': int(date_to),
                'description': 1, 'limit': page_size, 'offset': offset}
    if not isinstance(echo, dict) or any(echo.get(key) != value for key, value in expected.items()):
        raise RecoverySnapshotError('Paginação do extrato Deriv não corresponde à consulta')
    _validate_statement_transactions(transactions)
    return transactions


def _same_money(a, b):
    left, right = _decimal(a), _decimal(b)
    return left is not None and right is not None and abs(left - right) <= Decimal('0.02')


def _price_matches_order(price, order):
    actual = _decimal(price)
    if actual is None or actual <= 0:
        return False
    if order.buy_price is not None:
        return _same_money(actual, order.buy_price)
    reserved = _decimal(order.stake)
    # Deriv may fill below the reserved maximum; that is a possible purchase,
    # never evidence of absence.
    return reserved is not None and actual <= reserved


def _near_order_time(epoch, created_at, *, seconds=300):
    if isinstance(epoch, bool) or not isinstance(epoch, (int, float, str)):
        return False
    try:
        stamp = float(epoch)
    except (TypeError, ValueError):
        return False
    created = _as_utc(created_at)
    return created is not None and created.timestamp() - 30 <= stamp <= created.timestamp() + seconds


def _contract_matches_order(contract, order):
    if not _portfolio_contract_plausible(contract, order):
        return False
    return _price_matches_order(contract.get('buy_price'), order)


def _portfolio_contract_plausible(contract, order):
    if not isinstance(contract, dict):
        return False
    if str(contract.get('underlying_symbol', '')).upper() != str(order.symbol).upper():
        return False
    if str(contract.get('contract_type', '')).upper() != str(order.direction).upper():
        return False
    stamp = contract.get('purchase_time')
    if stamp is None:
        stamp = contract.get('date_start')
    return _near_order_time(stamp, order.created_at)


def _statement_buy_matches_order(transaction, order, app_id):
    if not isinstance(transaction, dict) or transaction.get('action_type') != 'buy':
        return False
    if str(transaction.get('app_id') or '') != str(app_id):
        return False
    if not _near_order_time(transaction.get('transaction_time'), order.created_at):
        return False
    amount = _decimal(transaction.get('amount'))
    if amount is None or not _price_matches_order(abs(amount), order):
        return False
    descriptor = ' '.join(str(transaction.get(key) or '') for key in ('shortcode', 'longcode')).upper()
    return str(order.symbol).upper() in descriptor and str(order.direction).upper() in descriptor


def _statement_buy_plausible(transaction, order, app_id):
    if not isinstance(transaction, dict) or transaction.get('action_type') != 'buy':
        return False
    if not _near_order_time(transaction.get('transaction_time'), order.created_at,
                            seconds=3600):
        return False
    amount = _decimal(transaction.get('amount'))
    if amount is None or abs(amount) <= 0:
        return False
    descriptor = ' '.join(str(transaction.get(key) or '')
                          for key in ('shortcode', 'longcode')).upper()
    same_asset = str(order.symbol).upper() in descriptor
    same_direction = str(order.direction).upper() in descriptor
    same_app = str(transaction.get('app_id') or '') == str(app_id)
    # A same-app or same-asset purchase with an irregular price is suspicious,
    # not proof that the original request was absent. Price-compatible trades
    # and records with missing descriptions are also retained as ambiguous.
    return (same_app or (same_asset and same_direction)
            or _price_matches_order(abs(amount), order) or not descriptor.strip())


def classify_unidentified_order(order, open_contracts, transactions, app_id):
    """Return (classification, contract_id, evidence).

    A unique statement match may be linked, optionally corroborated by the
    same Portfolio contract. A Portfolio-only candidate stays ambiguous. An
    absent result requires no plausible match in either complete source.
    """
    portfolio_plausible = [c for c in open_contracts
                           if _portfolio_contract_plausible(c, order)]
    portfolio_matches = [c for c in portfolio_plausible
                         if _contract_matches_order(c, order)]
    created = _as_utc(order.created_at)
    if created is None:
        return 'AMBIGUOUS', None, 'data local ausente'
    exact_buys = [t for t in transactions if _statement_buy_matches_order(t, order, app_id)]
    # Any price-compatible buy close to this reservation but missing the full
    # app/symbol/direction/time match keeps the decision ambiguous, even when
    # Portfolio also has one candidate. Never let one source mask another.
    plausible_buys = [item for item in transactions
                      if _statement_buy_plausible(item, order, app_id)]
    if any(item not in exact_buys for item in plausible_buys):
        return 'AMBIGUOUS', None, 'compra próxima sem identificação suficiente'
    if len(portfolio_plausible) != len(portfolio_matches):
        return 'AMBIGUOUS', None, 'posição compatível com preço ou dados incompletos'

    candidate_evidence = {}
    for contract in portfolio_matches:
        cid = str(contract.get('contract_id') or '')
        if not cid.isdigit():
            return 'AMBIGUOUS', None, 'posição sem identificador válido'
        candidate_evidence.setdefault(cid, contract)
    for transaction in exact_buys:
        cid = str(transaction.get('contract_id') or '')
        if not cid.isdigit():
            return 'AMBIGUOUS', None, 'compra sem identificador no extrato'
        candidate_evidence.setdefault(cid, {
            'contract_id': cid,
            'underlying_symbol': order.symbol,
            'contract_type': order.direction,
            'buy_price': str(abs(_decimal(transaction.get('amount')))),
            'purchase_time': transaction.get('transaction_time'),
            'date_start': transaction.get('transaction_time'),
        })
    if len(candidate_evidence) == 1:
        cid, evidence = next(iter(candidate_evidence.items()))
        if portfolio_matches and not exact_buys:
            return 'AMBIGUOUS', None, 'posição sem compra corroborada no extrato'
        return 'MATCH', cid, evidence
    if len(candidate_evidence) > 1:
        return 'AMBIGUOUS', None, 'portfolio e extrato apontam contratos diferentes'
    if plausible_buys:
        return 'AMBIGUOUS', None, 'compra próxima sem identificação suficiente'
    return 'ABSENT', None, 'portfolio e extrato completos sem compra compatível'


async def account_recovery_snapshot(mode, token, account_id, date_from, date_to):
    """Fetch a complete authenticated portfolio + statement; no trading calls."""
    from .demo import DemoTradingError, _response_for as demo_response, get_demo_ws_url
    from .real_broker import RealExecutionError, _response_for as real_response, get_real_ws_url

    if mode == 'demo':
        get_url, response_for, request_error = get_demo_ws_url, demo_response, DemoTradingError
    elif mode == 'real':
        get_url, response_for, request_error = get_real_ws_url, real_response, RealExecutionError
    else:
        raise RecoverySnapshotError('Modo de conta inválido')

    try:
        ws_url = await get_url(token, account_id)
        async with asyncio.timeout(35):
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(json.dumps({'portfolio': 1, 'req_id': 9810}))
                portfolio_message = await response_for(ws, 9810)
                if (not isinstance(portfolio_message, dict)
                    or portfolio_message.get('msg_type') != 'portfolio'
                    or portfolio_message.get('req_id') != 9810):
                    raise RecoverySnapshotError('Resposta de portfolio Deriv não corresponde à consulta')
                portfolio = portfolio_message.get('portfolio')
                contracts = portfolio.get('contracts') if isinstance(portfolio, dict) else None
                _validate_portfolio_contracts(contracts)

                transactions = []
                offset = 0
                page_size = 999
                for page in range(10):
                    req_id = 9820 + page
                    await ws.send(json.dumps({
                        'statement': 1,
                        'date_from': int(date_from),
                        'date_to': int(date_to),
                        'description': 1,
                        'limit': page_size,
                        'offset': offset,
                        'req_id': req_id,
                    }))
                    message = await response_for(ws, req_id)
                    page_items = _validated_statement_page(
                        message, req_id=req_id, offset=offset, page_size=page_size,
                        date_from=date_from, date_to=date_to)
                    if not page_items:
                        return {'contracts': contracts, 'transactions': transactions, 'complete': True}
                    transactions.extend(page_items)
                    offset += len(page_items)
                raise RecoverySnapshotError('Extrato excede o limite de verificação segura')
    except RecoverySnapshotError:
        raise
    except Exception as exc:
        if isinstance(exc, request_error):
            raise RecoverySnapshotError('Não foi possível confirmar a conta na Deriv') from None
        raise RecoverySnapshotError('Falha ou resposta incompleta da Deriv; bloqueios preservados') from None
