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
