"""Authenticated Deriv demo client.

The module rejects real channels. Proposal retrieval is read-only; buying is
available only on the demo channel and requires an explicit DEMO_ONLY marker.
"""
from __future__ import annotations

import asyncio
import json
import os
from decimal import Decimal, InvalidOperation

import httpx
import websockets

OTP_BASE = "https://api.derivws.com/trading/v1/options/accounts"
APP_ID = os.getenv("DERIV_CLIENT_ID", "34VU8L3LDiaENrDGVjqQP")


class DemoTradingError(Exception):
    """A controlled failure while using the authenticated demo channel."""


def _amount(value: str) -> float:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise DemoTradingError("Valor de stake inválido") from exc
    if not amount.is_finite() or amount < Decimal("2") or amount > Decimal("1000"):
        raise DemoTradingError("Stake demo fora dos limites permitidos")
    return float(amount)


def _validate_demo_url(url: str) -> str:
    if not isinstance(url, str) or "/trading/v1/options/ws/demo" not in url:
        raise DemoTradingError("A Deriv não retornou um canal demo; operação bloqueada")
    if "/ws/real" in url or "real" in url.split("?")[0].lower():
        raise DemoTradingError("Canal real detectado; operação bloqueada")
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
    except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
        raise DemoTradingError("Não foi possível abrir o canal demo da Deriv") from exc


async def _response_for(ws, req_id: int) -> dict:
    while True:
        message = json.loads(await ws.recv())
        if message.get("req_id") != req_id:
            continue
        if message.get("error"):
            raise DemoTradingError(str(message["error"].get("message", "Operação demo recusada")))
        return message


async def demo_proposal(token: str, account_id: str, *, symbol: str,
                        contract_type: str, stake: str, duration: int,
                        currency: str = "USD") -> dict:
    """Query a real demo-account proposal without buying a contract."""
    if contract_type not in {"CALL", "PUT"}:
        raise DemoTradingError("Tipo de contrato demo não autorizado")
    if duration not in {1, 3, 5}:
        raise DemoTradingError("Duração demo não autorizada")
    if not isinstance(symbol, str) or not symbol.isalnum() or not 2 <= len(symbol) <= 30:
        raise DemoTradingError("Símbolo inválido")

    ws_url = await get_demo_ws_url(token, account_id)
    request = {"proposal": 1, "amount": _amount(stake), "basis": "stake",
               "contract_type": contract_type, "currency": currency,
               "duration": duration, "duration_unit": "m",
               "underlying_symbol": symbol, "req_id": 7001}
    try:
        async with asyncio.timeout(20):
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10) as ws:
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
    except (TimeoutError, OSError, ConnectionError, ValueError, TypeError, KeyError) as exc:
        raise DemoTradingError("Não foi possível consultar proposal na conta demo") from exc


async def demo_buy(token: str, account_id: str, *, symbol: str,
                   contract_type: str, stake: str, duration: int,
                   confirm: str, currency: str = "USD") -> dict:
    """Buy one contract on the demo channel after explicit confirmation."""
    if confirm != "DEMO_ONLY":
        raise DemoTradingError("Confirmação demo obrigatória")
    if contract_type not in {"CALL", "PUT"} or duration not in {1, 3, 5}:
        raise DemoTradingError("Parâmetros de contrato demo não autorizados")
    if not isinstance(symbol, str) or not symbol.isalnum() or not 2 <= len(symbol) <= 30:
        raise DemoTradingError("Símbolo inválido")

    ws_url = await get_demo_ws_url(token, account_id)
    proposal_request = {"proposal": 1, "amount": _amount(stake), "basis": "stake",
                        "contract_type": contract_type, "currency": currency,
                        "duration": duration, "duration_unit": "m",
                        "underlying_symbol": symbol, "req_id": 7101}
    try:
        async with asyncio.timeout(20):
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(json.dumps(proposal_request))
                proposal = (await _response_for(ws, 7101)).get("proposal")
                if not isinstance(proposal, dict):
                    raise DemoTradingError("Resposta de proposal demo inválida")
                proposal_id, ask_price = str(proposal.get("id", "")), proposal.get("ask_price")
                if not proposal_id or ask_price is None:
                    raise DemoTradingError("Proposal demo sem identificador ou preço")
                await ws.send(json.dumps({"buy": proposal_id, "price": float(ask_price), "req_id": 7102}))
                buy = (await _response_for(ws, 7102)).get("buy")
                if not isinstance(buy, dict) or not buy.get("contract_id"):
                    raise DemoTradingError("A Deriv não confirmou a compra demo")
                return {"contract_id": str(buy["contract_id"]),
                        "buy_price": str(buy.get("buy_price", ask_price)),
                        "symbol": symbol, "contract_type": contract_type,
                        "duration_minutes": duration, "currency": currency,
                        "demo_only": True, "bought": True}
    except DemoTradingError:
        raise
    except (TimeoutError, OSError, ConnectionError, ValueError, TypeError, KeyError) as exc:
        raise DemoTradingError("Não foi possível comprar o contrato demo") from exc


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
    except (TimeoutError, OSError, ConnectionError, ValueError, TypeError, KeyError) as exc:
        raise DemoTradingError("Não foi possível consultar o contrato demo") from exc
