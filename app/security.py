"""OAuth PKCE, opaque-cookie sessions and authenticated token protection."""
import base64, hashlib, os, secrets
from datetime import timedelta
from urllib.parse import urlencode
from cryptography.fernet import Fernet
from .db import utcnow

CLIENT_ID = os.getenv('DERIV_CLIENT_ID', '34VU8L3LDiaENrDGVjqQP')
REDIRECT = os.getenv('DERIV_REDIRECT_URI', 'http://localhost:8000/auth/deriv/callback')
AUTH_URL = 'https://auth.deriv.com/oauth2/auth'
TOKEN_URL = 'https://auth.deriv.com/oauth2/token'
CIPHER_KEY = os.getenv('NEXORA_FERNET_KEY')

def cipher():
    if not CIPHER_KEY:
        raise RuntimeError('Configure NEXORA_FERNET_KEY (Fernet URL-safe key)')
    return Fernet(CIPHER_KEY.encode())

def encrypt(s): return cipher().encrypt(s.encode()).decode()
def decrypt(s): return cipher().decrypt(s.encode()).decode()
def hashsecret(s): return hashlib.sha256(s.encode()).hexdigest()
def pkce_challenge(verifier): return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
def build_authorize_url(state, verifier, *, register=False):
    params={'response_type':'code','client_id':CLIENT_ID,'redirect_uri':REDIRECT,
        'scope':'trade','state':state,'code_challenge':pkce_challenge(verifier),'code_challenge_method':'S256'}
    if register:
        # Deriv OAuth supports this prompt for first-time account registration.
        # Consent callback still returns to the registered NEXORA redirect_uri.
        params['prompt']='registration'
    return AUTH_URL + '?' + urlencode(params)
def make_login(): return secrets.token_urlsafe(32),secrets.token_urlsafe(64)
def make_session_id(): return secrets.token_urlsafe(32)
def cookie_options():
    public = os.getenv('NEXORA_PUBLIC_URL','').startswith('https://')
    return {'httponly':True,'secure':public,'samesite':'lax','path':'/'}
