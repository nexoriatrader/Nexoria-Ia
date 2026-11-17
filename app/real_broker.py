"""Isolated Deriv REAL Options account client (NOT wired to autopilot yet).

A real trade must never pass through demo_orders/demo_ledger. The caller must
durably reserve a real-order slot and enforce per-account risk limits BEFORE
calling buy_real_contract. Unknown broker replies require reconciliation.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, urlsplit

import httpx
import websockets

API_ACCOUNTS = "https://api.derivws.com/trading/v1/options/accounts"
APP_ID = os.getenv("DERIV_CLIENT_ID", "34VU8L3LDiaENrDGVjqQP")


class RealExecutionError(Exception):
    """Controlled real-account broker failure."""


class RealPurchaseUncertain(RealExecutionError):
    """A buy might have reached the broker. Never retry without reconciliation."""


def validate_real_ws_url(url: str) -> str:
    if not isinstance(url, str):
        raise RealExecutionError("Canal de conta real inválido")
    parsed = urlsplit(url)
    try:
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise RealExecutionError("OTP da conta real inválido") from exc
    if (parsed.scheme != "wss" or parsed.hostname != "api.derivws.com"
        or parsed.port not in (None, 443)
        or parsed.path != "/trading/v1/options/ws/real"
        or parsed.username is not None or parsed.password is not None
        or parsed.fragment or set(query) != {"otp"}
        or len(query.get("otp", [])) != 1 or not query["otp"][0]):
        raise RealExecutionError("A Deriv não confirmou um canal REAL oficial")
    return url


async def get_real_ws_url(token: str, account_id: str) -> str:
    if not isinstance(account_id, str) or not re.fullmatch(r"[A-Za-z0-9]{2,100}", account_id):
        raise RealExecutionError("Conta real inválida")
    if not isinstance(token, str) or not token:
        raise RealExecutionError("Autorização Deriv indisponível")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{API_ACCOUNTS}/{account_id}/otp",
                headers={"Authorization": f"Bearer {token}", "Deriv-App-ID": APP_ID},
            )
            response.raise_for_status()
            return validate_real_ws_url(response.json().get("data", {}).get("url"))
    except RealExecutionError:
        raise
    except (httpx.HTTPError, ValueError, AttributeError, TypeError) as exc:
        raise RealExecutionError("Não foi possível abrir canal REAL na Deriv") from exc


async def _response_for(ws, request_id: int) -> dict:
    while True:
        message = json.loads(await ws.recv())
        if not isinstance(message, dict):
            raise RealExecutionError("Resposta da Deriv inválida")
        if message.get("req_id") != request_id:
            continue
        if message.get("error"):
            raise RealExecutionError("A Deriv rejeitou a solicitação REAL")
        return message


async def real_account_balance(token: str, account_id: str) -> dict:
    """Read-only account balance, explicitly scoped to the real OTP endpoint."""
    url = await get_real_ws_url(token, account_id)
    try:
        async with asyncio.timeout(18):
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(json.dumps({"balance": 1, "req_id": 8401}))
                result = (await _response_for(ws, 8401)).get("balance")
                if not isinstance(result, dict):
                    raise RealExecutionError("Saldo REAL indisponível")
                amount = Decimal(str(result.get("balance")))
                currency = str(result.get("currency", ""))
                if not amount.is_finite() or not re.fullmatch(r"[A-Za-z0-9]{2,20}", currency):
                    raise RealExecutionError("Dados inválidos do saldo REAL")
                return {"balance": str(amount), "currency": currency, "account_mode": "real"}
    except RealExecutionError:
        raise
    except Exception as exc:
        raise RealExecutionError("Não foi possível consultar saldo REAL") from exc


async def buy_real_contract(
    token: str, account_id: str, *, symbol: str, contract_type: str,
    stake: str, duration: int, currency: str, min_payout_ratio: str,
    latest_buy_send_epoch: float | None = None,
    duration_options: tuple[int, ...] | None = None,
) -> dict:
    """Buy only after caller's durable REAL reservation and risk checks.

    Optional automatic expiry probes M1/M3/M5/M15 with read-only quotes, then
    fetches a fresh streaming proposal for the selected expiry before purchase.
    """
    if not re.fullmatch(r"[A-Za-z0-9_]{2,30}", symbol or ""):
        raise RealExecutionError("Ativo REAL inválido")
    if contract_type not in ("CALL", "PUT") or duration not in (1, 3, 5, 15):
        raise RealExecutionError("Contrato REAL inválido")
    if duration_options is not None:
        if (not isinstance(duration_options, tuple) or not duration_options
            or any(x not in (1,3,5,15) for x in duration_options)
            or len(duration_options)>4):
            raise RealExecutionError("Expirações automáticas REAL inválidas")
        duration_options=tuple(dict.fromkeys(duration_options))
    if not re.fullmatch(r"[A-Za-z0-9]{2,20}", currency or ""):
        raise RealExecutionError("Moeda REAL inválida")
    try:
        amount = Decimal(str(stake))
        minimum_ratio = Decimal(str(min_payout_ratio))
        if (not amount.is_finite() or amount < Decimal("0.50")
            or not minimum_ratio.is_finite() or minimum_ratio < 1
            or not math.isfinite(float(amount))):
            raise RealExecutionError("Valor ou payout REAL inválido")
    except (InvalidOperation, ValueError, OverflowError, TypeError) as exc:
        raise RealExecutionError("Valor ou payout REAL inválido") from exc

    # No shared account socket: private authorization and price are never cached.
    url = await get_real_ws_url(token, account_id)
    sent_buy = False
    try:
        async with asyncio.timeout(20):
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                if duration_options:
                    # One authenticated socket; never accept non-streaming
                    # proposal IDs for a purchase. They are quotes only.
                    outstanding={}
                    for i,minutes in enumerate(duration_options):
                        req_id=8500+i
                        outstanding[req_id]=minutes
                        await ws.send(json.dumps({
                            "proposal":1,"amount":float(amount),"basis":"stake",
                            "contract_type":contract_type,"currency":currency,
                            "duration":minutes,"duration_unit":"m",
                            "underlying_symbol":symbol,"req_id":req_id,
                        }))
                    eligible=[]
                    try:
                        budget=4.0
                        if latest_buy_send_epoch is not None:
                            budget=min(budget,max(0.0,latest_buy_send_epoch-time.time()-2.0))
                        async with asyncio.timeout(budget):
                            while outstanding:
                                response=json.loads(await ws.recv())
                                req_id=response.get("req_id")
                                if req_id not in outstanding:
                                    continue
                                minutes=outstanding.pop(req_id)
                                proposal_quote=response.get("proposal")
                                if response.get("error") or not isinstance(proposal_quote,dict):
                                    continue
                                try:
                                    ask_quote=Decimal(str(proposal_quote.get("ask_price")))
                                    pay_quote=Decimal(str(proposal_quote.get("payout")))
                                    if (ask_quote.is_finite() and 0<ask_quote<=amount
                                        and pay_quote.is_finite() and
                                        pay_quote>=ask_quote*minimum_ratio):
                                        eligible.append((pay_quote/ask_quote, minutes))
                                except (InvalidOperation,ValueError,TypeError):
                                    continue
                    except TimeoutError:
                        pass
                    if not eligible:
                        raise RealExecutionError(
                            "Nenhuma expiração REAL com payout válido na janela do sinal")
                    duration=max(eligible)[1]
                # Broker requires a fresh STREAMING proposal for the actual
                # contract. The preliminary automatic quotes are never bought.
                await ws.send(json.dumps({
                    "proposal": 1, "amount": float(amount), "basis": "stake",
                    "contract_type": contract_type, "currency": currency,
                    "duration": duration, "duration_unit": "m",
                    "underlying_symbol": symbol, "subscribe": 1, "req_id": 8402,
                }))
                proposal = (await _response_for(ws, 8402)).get("proposal")
                if not isinstance(proposal, dict) or not proposal.get("id"):
                    raise RealExecutionError("A proposta REAL não foi confirmada")
                ask = Decimal(str(proposal.get("ask_price")))
                payout = Decimal(str(proposal.get("payout")))
                if (not ask.is_finite() or ask <= 0 or ask > amount
                    or not payout.is_finite() or payout < ask * minimum_ratio):
                    raise RealExecutionError("Proposta REAL não atende aos limites autorizados")
                if latest_buy_send_epoch is not None and time.time() > latest_buy_send_epoch:
                    raise RealExecutionError("Sinal expirado antes da compra REAL")
                # From this point onward failures are uncertain: broker might buy.
                sent_buy = True
                await ws.send(json.dumps({
                    "buy": str(proposal["id"]), "price": float(ask),
                    "req_id": 8403,
                }))
                receipt = (await _response_for(ws, 8403)).get("buy")
                if not isinstance(receipt, dict) or not receipt.get("contract_id"):
                    raise RealPurchaseUncertain("Compra REAL enviada sem confirmação de contrato")
                return {
                    "contract_id": str(receipt["contract_id"]),
                    "buy_price": str(receipt.get("buy_price", ask)),
                    "symbol": symbol, "contract_type": contract_type,
                    "duration_minutes": duration, "currency": currency,
                    "account_mode": "real", "bought": True,
                    "broker_purchase_time": receipt.get("purchase_time"),
                }
    except RealPurchaseUncertain:
        raise
    except Exception as exc:
        if sent_buy:
            raise RealPurchaseUncertain(
                "Compra REAL com resultado incerto: bloquear novas entradas até reconciliar"
            ) from exc
        if isinstance(exc, RealExecutionError):
            raise
        raise RealExecutionError("Proposta REAL indisponível; compra não enviada") from exc


async def real_contract_status(token: str, account_id: str, contract_id: str) -> dict:
    """Read-only broker status to support a future REAL reconciliation worker."""
    if not isinstance(contract_id, str) or not contract_id.isdigit():
        raise RealExecutionError("ID de contrato REAL inválido")
    url = await get_real_ws_url(token, account_id)
    try:
        async with asyncio.timeout(20):
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(json.dumps({
                    "proposal_open_contract": 1, "contract_id": int(contract_id),
                    "req_id": 8404,
                }))
                contract = (await _response_for(ws, 8404)).get("proposal_open_contract")
                if not isinstance(contract, dict):
                    raise RealExecutionError("Status REAL indisponível")
                return {"account_mode": "real", "contract": contract}
    except RealExecutionError:
        raise
    except Exception as exc:
        raise RealExecutionError("Falha ao consultar contrato REAL") from exc
