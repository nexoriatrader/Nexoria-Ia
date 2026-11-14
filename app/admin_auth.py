"""Standalone owner-only administration, with no broker account privileges.

Password hash and HMAC session key are provided by Render environment variables;
never store credentials in Git, cookies, client JS or the database.
"""
import base64
import binascii
import hashlib
import hmac
import os
import secrets
import time

SESSION_SECONDS=8*3600
FAILURE_WINDOW=15*60
_failed={}


def configured():
    return bool(os.getenv('NEXORA_ADMIN_PASSWORD_HASH') and
                os.getenv('NEXORA_ADMIN_SESSION_SECRET'))


def verify_password(password):
    stored=os.getenv('NEXORA_ADMIN_PASSWORD_HASH','')
    try:
        kind,rounds,salt,expected=stored.split('$')
        rounds=int(rounds)
        if kind!='pbkdf2_sha256' or not 200000<=rounds<=1200000:
            return False
        actual=hashlib.pbkdf2_hmac('sha256',password.encode(),
                                  bytes.fromhex(salt),rounds)
        return hmac.compare_digest(actual,bytes.fromhex(expected))
    except (ValueError,TypeError,OverflowError):
        return False


def login_allowed(identity,now=None):
    now=time.time() if now is None else now
    recent=[x for x in _failed.get(identity,[]) if now-x<FAILURE_WINDOW]
    _failed[identity]=recent
    return len(recent)<5


def record_failed_login(identity,now=None):
    _failed.setdefault(identity,[]).append(time.time() if now is None else now)


def clear_failed_logins(identity):
    _failed.pop(identity,None)


def session_token(now=None):
    if not configured():
        raise RuntimeError('Admin não configurado')
    issued=int(time.time() if now is None else now)
    nonce=secrets.token_urlsafe(20)
    data=f'{issued}:{nonce}'
    signature=hmac.new(os.environ['NEXORA_ADMIN_SESSION_SECRET'].encode(),
                       data.encode(),hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f'{data}:{signature}'.encode()).decode()


def valid_session(token,now=None):
    if not configured() or not token or len(token)>512:
        return False
    try:
        issued,nonce,signature=base64.urlsafe_b64decode(
            token.encode()).decode().split(':')
        issued=int(issued)
        now=time.time() if now is None else now
        if not nonce or issued>now+30 or now-issued>SESSION_SECONDS:
            return False
        data=f'{issued}:{nonce}'
        expected=hmac.new(os.environ['NEXORA_ADMIN_SESSION_SECRET'].encode(),
                          data.encode(),hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature,expected)
    except (ValueError,UnicodeError,TypeError,OverflowError,binascii.Error):
        return False
