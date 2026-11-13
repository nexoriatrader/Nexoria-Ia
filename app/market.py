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


# Two reusable, read-only public Deriv sessions. V2 scans previously opened
# a new WebSocket for every contracts/candles query, overwhelming the handshake
# path. Serialize requests on each session, with unique IDs and fail-closed reset.
_public_sessions = [None, None]
_public_locks = [asyncio.Lock(), asyncio.Lock()]
_public_slot = 0
_public_request_limit = asyncio.Semaphore(2)


async def public_request(payload: dict, *, expected: str, timeout: float = 12.0) -> dict:
    import logging
    import websockets
    from websockets.exceptions import ConnectionClosed, WebSocketException

    global _public_slot
    logger = logging.getLogger("nexora.market")
    async with _public_request_limit:
        slot = _public_slot % len(_public_sessions)
        _public_slot += 1
        async with _public_locks[slot]:
            for attempt in range(2):
                try:
                    async with asyncio.timeout(timeout):
                        ws = _public_sessions[slot]
                        if ws is None:
                            ws = await websockets.connect(
                                PUBLIC_WS, open_timeout=8, ping_interval=20,
                                ping_timeout=15, close_timeout=3, max_size=1_500_000
                            )
                            _public_sessions[slot] = ws
                        # A session handles only one request at a time.
                        await ws.send(json.dumps({**payload, 'req_id': 419}))
                        while True:
                            msg = json.loads(await ws.recv())
                            if msg.get('req_id') != 419:
                                continue
                            if msg.get('error'):
                                raise MarketError(str(msg['error'].get('message', 'Erro Deriv'))[:200])
                            if msg.get('msg_type') == expected or expected in msg:
                                return msg
                except asyncio.CancelledError:
                    ws = _public_sessions[slot]
                    _public_sessions[slot] = None
                    if ws is not None:
                        try:
                            await ws.close()
                        except Exception:
                            pass
                    raise
                except MarketError:
                    # A broker response can be an explicit business rejection;
                    # don't blindly retry or change any trade authorization.
                    raise
                except (TimeoutError, OSError, ConnectionError, ConnectionClosed,
                        WebSocketException, ValueError) as exc:
                    ws = _public_sessions[slot]
                    _public_sessions[slot] = None
                    if ws is not None:
                        try:
                            async with asyncio.timeout(2):
                                await ws.close()
                        except Exception:
                            pass
                    detail = type(exc).__name__
                    if isinstance(exc, ConnectionClosed):
                        detail += f"(code={getattr(exc.rcvd, 'code', 'unknown')})"
                    response = getattr(exc, 'response', None)
                    status = getattr(response, 'status_code', None)
                    if isinstance(status, int):
                        detail += f"(http_status={status})"
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
