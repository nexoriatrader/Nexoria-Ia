"""Authenticated Deriv demo client.

The module rejects real channels. Balance and proposal retrieval are read-only;
buying is available only on the demo channel and requires explicit confirmation.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
from urllib.parse import parse_qs, urlsplit
from decimal import Decimal, InvalidOperation
from contextlib import AsyncExitStack

import httpx
import websockets

OTP_BASE = "https://api.derivws.com/trading/v1/options/accounts"
APP_ID = os.getenv("DERIV_CLIENT_ID", "34VU8L3LDiaENrDGVjqQP")
# Only the validated ISO currency code (no balance, credentials, or OTP) is
# cached briefly to avoid an extra WebSocket round trip on time-critical buys.
_demo_currency_cache: dict[str, tuple[float,str]] = {}


def cached_demo_currency(account_id: str) -> str | None:
    cached=_demo_currency_cache.get(account_id)
    if cached and time.monotonic()-cached[0]<120:
        return cached[1]
    return None


def remember_demo_currency(account_id: str, currency: str) -> None:
    _demo_currency_cache[account_id]=(time.monotonic(),_currency(currency))


class DemoTradingError(Exception):
    """A controlled failure while using the authenticated demo channel."""


class DemoPreBuyDeadlineExceeded(DemoTradingError):
    """No buy was sent because the allowed entry window expired."""


def _amount(value: str) -> float:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise DemoTradingError("Valor de stake inválido") from exc
    if not amount.is_finite() or amount < Decimal("0.50"):
        raise DemoTradingError("Stake DEMO inválida ou abaixo do mínimo")
    # The broker is authoritative for its maximum permitted stake and funds.
    # Reject unrepresentable numbers instead of imposing an artificial cap.
    try:
        value_float = float(amount)
    except (OverflowError, ValueError):
        raise DemoTradingError("Stake DEMO fora da faixa numérica suportada") from None
    if not math.isfinite(value_float):
        raise DemoTradingError("Stake DEMO fora da faixa numérica suportada")
    return value_float


def _currency(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9]{2,20}", value):
        raise DemoTradingError("Moeda da conta demo inválida")
    return value


def _balance_values(message: dict) -> tuple[str, str]:
    data = message.get("balance")
    if not isinstance(data, dict) or data.get("balance") is None:
        raise DemoTradingError("Resposta de saldo demo inválida")
    try:
        amount = Decimal(str(data["balance"]))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise DemoTradingError("Saldo demo inválido") from exc
    if not amount.is_finite():
        raise DemoTradingError("Saldo demo inválido")
    return str(amount), _currency(data.get("currency"))


def _validate_demo_url(url: str) -> str:
    """Accept only the official TLS demo endpoint with one non-empty OTP."""
    if not isinstance(url, str):
        raise DemoTradingError("Canal demo inválido")
    parsed = urlsplit(url)
    try:
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise DemoTradingError("OTP demo inválido") from exc
    if (
        parsed.scheme != "wss"
        or parsed.hostname != "api.derivws.com"
        or parsed.port not in (None, 443)
        or parsed.path != "/trading/v1/options/ws/demo"
        or parsed.username is not None
        or parsed.password is not None
        or set(query) != {"otp"}
        or len(query.get("otp", [])) != 1
        or not query["otp"][0]
        or parsed.fragment
    ):
        raise DemoTradingError("A Deriv não retornou um canal demo oficial; operação bloqueada")
    return url


async def get_demo_ws_url(token: str, account_id: str) -> str:
    """Obtain and validate the short-lived authenticated demo WebSocket URL."""
    url = f"{OTP_BASE}/{account_id}/otp"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {token}", "Deriv-App-ID": APP_ID},
            )
            response.raise_for_status()
            payload = response.json()
            return _validate_demo_url(payload.get("data", {}).get("url"))
    except DemoTradingError:
        raise
    except Exception as exc:
        raise DemoTradingError("Não foi possível abrir o canal demo da Deriv") from exc


async def _response_for(ws, req_id: int) -> dict:
    while True:
        message = json.loads(await ws.recv())
        if not isinstance(message, dict):
            raise DemoTradingError("Resposta da Deriv inválida")
        if message.get("req_id") != req_id:
            continue
        if message.get("error"):
            error = message["error"]
            exc = DemoTradingError(str(error.get("message", "Operação demo recusada")))
            # Broker error code is diagnostic metadata; never log the raw response,
            # OTP, account identifier, authorization token or arbitrary message.
            code = error.get("code")
            exc.deriv_code = code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_]{1,64}", code) else "UNSPECIFIED"
            raise exc
        return message


async def _read_balance(ws, req_id: int) -> tuple[str, str]:
    await ws.send(json.dumps({"balance": 1, "req_id": req_id}))
    return _balance_values(await _response_for(ws, req_id))


async def demo_account_balance(token: str, account_id: str) -> dict:
    """Read the official balance and currency from the authenticated demo channel."""
    ws_url = await get_demo_ws_url(token, account_id)
    try:
        async with asyncio.timeout(20):
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10) as ws:
                balance, currency = await _read_balance(ws, 7301)
                remember_demo_currency(account_id,currency)
                return {"balance": balance, "currency": currency, "demo_only": True}
    except DemoTradingError:
        raise
    except Exception as exc:
        raise DemoTradingError("Não foi possível consultar o saldo da conta demo") from exc


async def demo_proposal(token: str, account_id: str, *, symbol: str,
                        contract_type: str, stake: str, duration: int,
                        currency: str | None = None) -> dict:
    """Query a real demo-account proposal without buying a contract."""
    if contract_type not in {"CALL", "PUT"}:
        raise DemoTradingError("Tipo de contrato demo não autorizado")
    if duration not in {1, 3, 5, 15}:
        raise DemoTradingError("Duração demo não autorizada")
    if not isinstance(symbol, str) or not re.fullmatch(r'[A-Za-z0-9_]{2,30}',symbol):
        raise DemoTradingError("Símbolo inválido")

    ws_url = await get_demo_ws_url(token, account_id)
    try:
        async with asyncio.timeout(20):
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10) as ws:
                if currency is None:
                    currency=cached_demo_currency(account_id)
                    if currency is None:
                        _, currency = await _read_balance(ws, 7000)
                        remember_demo_currency(account_id,currency)
                currency = _currency(currency)
                request = {"proposal": 1, "amount": _amount(stake), "basis": "stake",
                           "contract_type": contract_type, "currency": currency,
                           "duration": duration, "duration_unit": "m",
                           "underlying_symbol": symbol, "req_id": 7001}
                await ws.send(json.dumps(request))
                message = await _response_for(ws, 7001)
                proposal = message.get("proposal")
                if not isinstance(proposal, dict):
                    raise DemoTradingError("Resposta de proposal demo inválida")
                return {"proposal_id": str(proposal.get("id", "")),
                        "ask_price": str(proposal.get("ask_price", "")),
                        "payout": str(proposal.get("payout", "")),
                        "currency": str(proposal.get("currency", currency)),
                        "symbol": symbol, "contract_type": contract_type,
                        "duration_minutes": duration, "demo_only": True, "bought": False}
    except DemoTradingError:
        raise
    except Exception as exc:
        raise DemoTradingError("Não foi possível consultar proposal na conta demo") from exc



async def demo_expiry_quotes(token: str, account_id: str, *, symbol: str,
                             contract_type: str, stake: str,
                             durations=(1, 3, 5, 15)) -> dict:
    """Read-only multi-expiry DEMO quotes over ONE authenticated connection.

    Each expiry has its own req_id. A failed or missing quote is excluded, not
    interpreted as a tradable contract. Purchases still make a fresh proposal
    and enforce the configured minimum payout.
    """
    if contract_type not in {"CALL", "PUT"}:
        raise DemoTradingError("Direção DEMO inválida")
    if not isinstance(symbol, str) or not re.fullmatch(r'[A-Za-z0-9_]{2,30}', symbol):
        raise DemoTradingError("Ativo DEMO inválido")
    durations=tuple(dict.fromkeys(int(x) for x in durations))
    if not durations or any(x not in (1,3,5,15) for x in durations):
        raise DemoTradingError("Expirações DEMO inválidas")
    url=await get_demo_ws_url(token,account_id)
    async with websockets.connect(url,ping_interval=20,ping_timeout=10) as ws:
        currency=cached_demo_currency(account_id)
        if currency is None:
            _,currency=await _read_balance(ws,7200)
            remember_demo_currency(account_id,currency)
        currency=_currency(currency)
        pending={}
        for n,duration in enumerate(durations):
            req_id=7210+n
            pending[req_id]=duration
            await ws.send(json.dumps({
                "proposal":1,"amount":_amount(stake),"basis":"stake",
                "contract_type":contract_type,"currency":currency,
                "duration":duration,"duration_unit":"m",
                "underlying_symbol":symbol,"req_id":req_id
            }))
        offers={}
        failures={}
        while pending:
            message=json.loads(await ws.recv())
            if not isinstance(message,dict):
                continue
            req_id=message.get("req_id")
            if req_id not in pending:
                continue
            duration=pending.pop(req_id)
            if message.get("error"):
                code=message["error"].get("code","UNSPECIFIED")
                failures[duration]=code if isinstance(code,str) and re.fullmatch(r"[A-Za-z0-9_]{1,64}",code) else "UNSPECIFIED"
                continue
            proposal=message.get("proposal")
            if not isinstance(proposal,dict):
                failures[duration]="INVALID_PROPOSAL"
                continue
            offers[duration]={
                "ask_price":str(proposal.get("ask_price","")),
                "payout":str(proposal.get("payout","")),
                "currency":str(proposal.get("currency",currency))
            }
        return {"offers":offers,"failures":failures}


# V2.4 only: an authenticated, warmed DEMO WebSocket can be acquired
# before a candle opens. No buy or proposal is sent during warming.
# Memory-only: a Render restart drops all sessions.
_demo_ready_sockets: dict[str, tuple[float,str,object]] = {}
_demo_socket_locks: dict[str,asyncio.Lock] = {}


def _demo_session_lock(account_id: str) -> asyncio.Lock:
    return _demo_socket_locks.setdefault(account_id,asyncio.Lock())


async def _close_demo_socket(account_id: str) -> None:
    entry=_demo_ready_sockets.pop(account_id,None)
    if entry is not None:
        try:
            await asyncio.wait_for(entry[2].close(),timeout=2)
        except Exception:
            pass


async def prepare_demo_buy_socket(token: str, account_id: str) -> bool:
    """Warm the official DEMO channel well BEFORE the intended entry boundary."""
    async with _demo_session_lock(account_id):
        old=_demo_ready_sockets.get(account_id)
        if old and time.monotonic()-old[0]<90 and old[1]==token and not old[2].close_code:
            return True
        await _close_demo_socket(account_id)
        ws=None
        try:
            url=await get_demo_ws_url(token,account_id)
            ws=await asyncio.wait_for(
                websockets.connect(url,ping_interval=20,ping_timeout=10),timeout=8)
            currency=cached_demo_currency(account_id)
            if currency is None:
                _,currency=await asyncio.wait_for(_read_balance(ws,7100),timeout=6)
                remember_demo_currency(account_id,currency)
            _demo_ready_sockets[account_id]=(time.monotonic(),token,ws)
            return True
        except Exception:
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass
            return False


async def _take_warmed_demo_socket(token: str, account_id: str):
    # Caller must hold _demo_session_lock. Single-use to prevent interleaved
    # proposal and buy responses across competing automatic orders.
    entry=_demo_ready_sockets.pop(account_id,None)
    if not entry:
        return None
    created,owner,ws=entry
    if owner==token and time.monotonic()-created<90 and not ws.close_code:
        return ws
    try:
        await ws.close()
    except Exception:
        pass
    return None


async def demo_buy(token: str, account_id: str, *, symbol: str,
                   contract_type: str, stake: str, duration: int,
                   confirm: str, currency: str | None = None,
                   min_payout_ratio: str | None = None,
                   latest_buy_send_epoch: float | None = None) -> dict:
    """Buy one contract on the demo channel after explicit confirmation."""
    if confirm != "DEMO_ONLY":
        raise DemoTradingError("Confirmação demo obrigatória")
    if contract_type not in {"CALL", "PUT"} or duration not in {1, 3, 5, 15}:
        raise DemoTradingError("Parâmetros de contrato demo não autorizados")
    if not isinstance(symbol, str) or not re.fullmatch(r'[A-Za-z0-9_]{2,30}',symbol):
        raise DemoTradingError("Símbolo inválido")

    # The timed V2.4 path consumes a session warmed while the signal was
    # prepared. With no prepared connection it attempts a cold DEMO channel,
    # still strictly bound by the original send deadline.
    async with _demo_session_lock(account_id):
        warm=await _take_warmed_demo_socket(token,account_id) if latest_buy_send_epoch is not None else None
        ws=warm
        async with AsyncExitStack() as stack:
            try:
                async with asyncio.timeout(20):
                    if ws is None:
                        ws_url=await get_demo_ws_url(token,account_id)
                        ws=await stack.enter_async_context(
                            websockets.connect(ws_url,ping_interval=20,ping_timeout=10))
                    if currency is None:
                        currency=cached_demo_currency(account_id)
                        if currency is None:
                            _, currency = await _read_balance(ws, 7100)
                            remember_demo_currency(account_id,currency)
                    currency = _currency(currency)
                    # Deriv v1/options API: only a streamed proposal ID may be
                    # purchased. Keep preflight demo_proposal() read-only.
                    proposal_request = {"proposal": 1, "amount": _amount(stake), "basis": "stake",
                                       "contract_type": contract_type, "currency": currency,
                                       "duration": duration, "duration_unit": "m",
                                       "underlying_symbol": symbol, "subscribe": 1, "req_id": 7101}
                    await ws.send(json.dumps(proposal_request))
                    proposal = (await _response_for(ws, 7101)).get("proposal")
                    if not isinstance(proposal, dict):
                        raise DemoTradingError("Resposta de proposal demo inválida")
                    proposal_id, ask_price = str(proposal.get("id", "")), proposal.get("ask_price")
                    if min_payout_ratio is not None:
                        # V2-only fail-closed check on the ACTUAL purchase proposal;
                        # the preliminary quote may change before buying.
                        try:
                            ratio=Decimal(str(min_payout_ratio))
                            payout=Decimal(str(proposal["payout"]))
                            proposal_ask=Decimal(str(ask_price))
                        except (InvalidOperation,TypeError,ValueError,KeyError) as exc:
                            raise DemoTradingError("Payout da proposta DEMO inválido") from exc
                        if (not ratio.is_finite() or ratio<1 or not payout.is_finite()
                            or not proposal_ask.is_finite() or proposal_ask<=0
                            or payout<proposal_ask*ratio):
                            raise DemoTradingError("Proposta DEMO não atende ao payout mínimo")
                    if not proposal_id or ask_price is None:
                        raise DemoTradingError("Proposal demo sem identificador ou preço")
                    # Never accept a quote whose purchase cost exceeds the guarded stake.
                    try:
                        price = Decimal(str(ask_price))
                    except (InvalidOperation, ValueError, TypeError) as exc:
                        raise DemoTradingError("Preço demo inválido") from exc
                    if not price.is_finite() or price <= 0 or price > Decimal(str(stake)):
                        raise DemoTradingError("Preço da proposta ultrapassa o valor autorizado")
                    # Fail closed immediately before the actual outbound buy request.
                    # The broker's processing/confirmation time is outside our control.
                    if latest_buy_send_epoch is not None and time.time()>latest_buy_send_epoch:
                        raise DemoPreBuyDeadlineExceeded("Janela de entrada M5 expirou antes do envio")
                    if latest_buy_send_epoch is not None:
                        print("V2.4 DEMO buy dispatch offset_s=%.3f deadline=3.000" %
                              (time.time()-(latest_buy_send_epoch-3.0)))
                    await ws.send(json.dumps({"buy": proposal_id, "price": float(price), "req_id": 7102}))
                    buy = (await _response_for(ws, 7102)).get("buy")
                    if not isinstance(buy, dict) or not buy.get("contract_id"):
                        raise DemoTradingError("A Deriv não confirmou a compra demo")
                    return {"contract_id": str(buy["contract_id"]),
                            "buy_price": str(buy.get("buy_price", ask_price)),
                            "symbol": symbol, "contract_type": contract_type,
                            "duration_minutes": duration, "currency": currency,
                            "broker_purchase_time": (buy.get("purchase_time")
                                                     if isinstance(buy.get("purchase_time"),(int,float)) else None),
                            "broker_start_time": (buy.get("start_time")
                                                  if isinstance(buy.get("start_time"),(int,float)) else None),
                            "demo_only": True, "bought": True}
            except DemoTradingError:
                raise
            except Exception as exc:
                raise DemoTradingError("Não foi possível comprar o contrato demo") from exc
            finally:
                if warm is not None:
                    try:
                        await warm.close()
                    except Exception:
                        pass


async def demo_contract_status(token: str, account_id: str, contract_id: str) -> dict:
    """Read the latest status of a demo contract without changing it."""
    if not isinstance(contract_id, str) or not contract_id.isdigit():
        raise DemoTradingError("Identificador de contrato inválido")
    ws_url = await get_demo_ws_url(token, account_id)
    try:
        async with asyncio.timeout(20):
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(json.dumps({"proposal_open_contract": 1,
                                          "contract_id": int(contract_id), "req_id": 7201}))
                contract = (await _response_for(ws, 7201)).get("proposal_open_contract")
                if not isinstance(contract, dict):
                    raise DemoTradingError("Status do contrato demo inválido")
                return {"demo_only": True, "contract": contract}
    except DemoTradingError:
        raise
    except Exception as exc:
        raise DemoTradingError("Não foi possível consultar o contrato demo") from exc
