"""Strictly read-only access to a Deriv real Options account balance.

No proposal, buy, sell, subscription, or trading commands are available here.
Requires explicit real account classification from the OAuth account-list response.
"""
import asyncio
import json
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit, parse_qs

import httpx
import websockets

from .demo import _balance_values


class RealBalanceError(Exception):
    pass


def validate_real_ws_url(url):
    if not isinstance(url,str):
        raise RealBalanceError('Canal real inválido')
    parts=urlsplit(url)
    try:
        query=parse_qs(parts.query,keep_blank_values=True,strict_parsing=True)
    except ValueError as exc:
        raise RealBalanceError('Autenticação da conta real inválida') from exc
    if (parts.scheme!='wss' or parts.hostname!='api.derivws.com'
        or parts.port not in (None,443) or parts.username is not None
        or parts.password is not None or parts.path!='/trading/v1/options/ws/real'
        or set(query)!={'otp'} or len(query.get('otp',[]))!=1
        or not query['otp'][0] or parts.fragment):
        raise RealBalanceError('Canal recebido não é o oficial de conta real')
    return url


async def fetch_real_balance(token,account_id):
    if not isinstance(account_id,str) or not re.fullmatch(r'[A-Za-z0-9]{2,100}',account_id):
        raise RealBalanceError('Identificador da conta real inválido')
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response=await client.post(
                f'https://api.derivws.com/trading/v1/options/accounts/{account_id}/otp',
                headers={'Authorization':f'Bearer {token}'})
            response.raise_for_status()
            ws_url=validate_real_ws_url(response.json()['data']['url'])
        async with asyncio.timeout(20):
            async with websockets.connect(ws_url,ping_interval=20,ping_timeout=10) as ws:
                # The only command transmitted on a real-money channel is read-only.
                await ws.send(json.dumps({'balance':1,'req_id':8901}))
                while True:
                    reply=json.loads(await ws.recv())
                    if not isinstance(reply,dict) or reply.get('req_id')!=8901:
                        continue
                    if reply.get('error'):
                        raise RealBalanceError('A Deriv não autorizou a leitura do saldo real')
                    amount,currency=_balance_values(reply)
                    return {'available':True,'balance':amount,'currency':currency,
                            'account_id':account_id,'reason':None}
    except RealBalanceError:
        raise
    except Exception as exc:
        raise RealBalanceError('Não foi possível consultar o saldo real nesta sessão') from exc


def parse_real_profit_transactions(transactions, start_epoch, end_epoch):
    """Only settled transactions with explicit sell timestamps count as today's P/L."""
    if not isinstance(transactions,list):
        raise RealBalanceError('Histórico de resultados reais inválido')
    from decimal import ROUND_HALF_UP
    total=Decimal('0')
    for tx in transactions:
        if not isinstance(tx,dict):
            raise RealBalanceError('Contrato real inválido')
        try:
            sold=int(tx['sell_time'])
            buy=Decimal(str(tx['buy_price']))
            sell=Decimal(str(tx['sell_price']))
        except (KeyError,ValueError,TypeError,InvalidOperation) as exc:
            raise RealBalanceError('Resultado real incompleto; cálculo não disponível') from exc
        if not buy.is_finite() or not sell.is_finite():
            raise RealBalanceError('Resultado real inválido')
        if start_epoch<=sold<=end_epoch:
            total+=sell-buy
    return total.quantize(Decimal('0.01'),rounding=ROUND_HALF_UP)


async def fetch_real_daily_profit(token,account_id,start_epoch,end_epoch):
    """Deriv real-account profit_table READ ONLY, paginated and fail-closed.

    An incomplete response is never displayed as zero. No trading requests are
    sent through this real-money WebSocket.
    """
    if not isinstance(account_id,str) or not re.fullmatch(r'[A-Za-z0-9]{2,100}',account_id):
        raise RealBalanceError('Identificador da conta real inválido')
    if not isinstance(start_epoch,int) or not isinstance(end_epoch,int) or start_epoch>end_epoch:
        raise RealBalanceError('Janela de resultados inválida')
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response=await client.post(
                f'https://api.derivws.com/trading/v1/options/accounts/{account_id}/otp',
                headers={'Authorization':f'Bearer {token}'})
            response.raise_for_status()
            ws_url=validate_real_ws_url(response.json()['data']['url'])
        async with asyncio.timeout(35):
            async with websockets.connect(ws_url,ping_interval=20,ping_timeout=10) as ws:
                # The balance response provides the currency of this exact real account.
                await ws.send(json.dumps({'balance':1,'req_id':8910}))
                while True:
                    reply=json.loads(await ws.recv())
                    if not isinstance(reply,dict) or reply.get('req_id')!=8910:
                        continue
                    if reply.get('error'):
                        raise RealBalanceError('A Deriv não autorizou a consulta da conta real')
                    _,currency=_balance_values(reply)
                    break
                total=Decimal('0')
                for offset in range(0,5000,500):
                    await ws.send(json.dumps({'profit_table':1,
                        'date_from':str(start_epoch),'date_to':str(end_epoch),
                        'limit':500,'offset':offset,'sort':'DESC','req_id':8911+offset}))
                    while True:
                        reply=json.loads(await ws.recv())
                        if not isinstance(reply,dict) or reply.get('req_id')!=8911+offset:
                            continue
                        if reply.get('error'):
                            raise RealBalanceError('A Deriv não autorizou os resultados reais')
                        payload=reply.get('profit_table')
                        if not isinstance(payload,dict):
                            raise RealBalanceError('Resposta de resultados reais inválida')
                        tx=payload.get('transactions')
                        if not isinstance(tx,list) or len(tx)>500:
                            raise RealBalanceError('Lista de operações reais inválida')
                        total+=parse_real_profit_transactions(tx,start_epoch,end_epoch)
                        break
                    if len(tx)<500:
                        return {'available':True,'pnl':str(total.quantize(Decimal('0.01'))),
                                'currency':currency,'account_id':account_id,
                                'source':'DERIV_REAL_PROFIT_TABLE','reason':None}
                raise RealBalanceError('Histórico real extenso; resultado indisponível até consulta completa')
    except RealBalanceError:
        raise
    except Exception as exc:
        raise RealBalanceError('Não foi possível consultar o resultado real nesta sessão') from exc
