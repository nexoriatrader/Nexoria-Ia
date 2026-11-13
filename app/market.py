"""Deriv public market research client. Read-only: no authenticated trading commands."""
import asyncio
import json
from decimal import Decimal, InvalidOperation

PUBLIC_WS = "wss://api.derivws.com/trading/v1/options/ws/public"


class MarketError(Exception):
    pass


def parse_quote(message: dict) -> dict:
    if message.get("error"):
        error = message["error"]
        raise MarketError(str(error.get("message", "Proposta não disponível")))
    proposal = message.get("proposal")
    if not isinstance(proposal, dict):
        raise MarketError("Deriv não retornou uma proposta válida")
    try:
        ask = Decimal(str(proposal["ask_price"]))
        payout = Decimal(str(proposal["payout"]))
    except (KeyError, InvalidOperation, ValueError, TypeError):
        raise MarketError("Cotação sem preço ou pagamento válido") from None
    if not ask.is_finite() or not payout.is_finite() or ask <= 0 or payout < 0:
        raise MarketError("Cotação com valores inválidos")
    return {
        'ask_price':str(ask), 'payout':str(payout),
        'potential_net_profit':str(payout - ask),
        'return_on_stake_pct':str(((payout - ask) / ask * 100).quantize(Decimal('0.01'))),
        'currency':str(proposal.get('currency', 'USD')),
        'indicative_only':True,
        'source':'Deriv proposal',
    }


# Shared cap prevents a scan from opening many short-lived WebSockets at once.
# One retry for transient network closures; broker errors are never retried.
_public_request_limit = asyncio.Semaphore(2)


async def public_request(payload: dict, *, expected: str, timeout: float = 12.0) -> dict:
    import logging
    import websockets
    from websockets.exceptions import ConnectionClosed, WebSocketException

    logger = logging.getLogger("nexora.market")
    async with _public_request_limit:
        for attempt in range(2):
            try:
                async with asyncio.timeout(timeout):
                    async with websockets.connect(
                        PUBLIC_WS, open_timeout=8, ping_interval=20,
                        ping_timeout=15, close_timeout=3, max_size=1_500_000
                    ) as ws:
                        await ws.send(json.dumps({**payload, 'req_id': 419}))
                        while True:
                            msg = json.loads(await ws.recv())
                            if msg.get('req_id') != 419:
                                continue
                            if msg.get('error'):
                                error = msg['error']
                                # Remote API errors are not transient transport failures.
                                raise MarketError(str(error.get('message', 'Erro Deriv'))[:200])
                            if msg.get('msg_type') == expected or expected in msg:
                                return msg
            except asyncio.CancelledError:
                raise
            except MarketError:
                raise
            except (TimeoutError, OSError, ConnectionError, ConnectionClosed, WebSocketException) as exc:
                detail = type(exc).__name__
                if isinstance(exc, ConnectionClosed):
                    detail += f"(code={getattr(exc.rcvd, 'code', 'unknown')})"
                logger.warning(
                    "Deriv public request interrupted: endpoint=%s attempt=%s/2 error=%s",
                    expected, attempt + 1, detail
                )
                if attempt == 1:
                    raise MarketError('Falha de conexão com Deriv (' + detail + ')') from exc
                await asyncio.sleep(0.8)
    raise MarketError('Falha inesperada na consulta pública Deriv')


def parse_symbols(message: dict) -> list[dict]:
    items = message.get('active_symbols', [])
    if not isinstance(items, list):
        return []
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        symbol = item.get('symbol') or item.get('underlying_symbol')
        if isinstance(symbol,str) and 2 <= len(symbol) <= 30:
            result.append({'symbol':symbol,'display_name':str(item.get('display_name',symbol))[:100]})
    return result


def parse_contract_types(message: dict) -> list[str]:
    raw=message.get('contracts_for')
    if isinstance(raw,dict):
        raw=raw.get('available',[])
    if not isinstance(raw,list):
        return []
    return sorted(set(item['contract_type'] for item in raw if isinstance(item,dict) and isinstance(item.get('contract_type'),str)))


async def discover_symbols() -> list[dict]:
    return parse_symbols(await public_request({'active_symbols':'brief'},expected='active_symbols'))


async def discover_contracts(symbol: str) -> list[str]:
    return parse_contract_types(await public_request({'contracts_for':symbol},expected='contracts_for'))


async def discover_contract_details(symbol: str) -> list[dict]:
    """Read-only authoritative contract durations and types for one symbol."""
    response=await public_request({'contracts_for':symbol},expected='contracts_for')
    data=response.get('contracts_for')
    contracts=data.get('available') if isinstance(data,dict) else None
    if not isinstance(contracts,list):
        raise MarketError('Deriv não informou as condições dos contratos')
    return [item for item in contracts if isinstance(item,dict)]



async def indicative_proposal(symbol: str, *, stake: str, contract_type: str = 'CALL', duration: int = 5,
                              currency: str = 'USD') -> dict:
    if contract_type not in ('CALL','PUT'):
        raise MarketError('Contrato não autorizado para pesquisa')
    try:
        amount=Decimal(stake)
        if not amount.is_finite() or amount < Decimal('0.50') or amount > Decimal('1000'):
            raise ValueError()
    except (InvalidOperation,ValueError):
        raise MarketError('Valor fora dos limites de pesquisa') from None
    if duration not in (1,3,5,15):
        raise MarketError('Duração não permitida para pesquisa')
    message=await public_request({
        'proposal':1, 'amount':float(amount), 'basis':'stake',
        'contract_type':contract_type, 'currency':currency,
        'duration':duration, 'duration_unit':'m',
        'underlying_symbol':symbol,
    },expected='proposal')
    return {**parse_quote(message),'symbol':symbol,'contract_type':contract_type,'duration_minutes':duration}
