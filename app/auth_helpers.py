"""Only accept demonstrably demo-labeled Deriv accounts; never infer real accounts as demo."""
def extract_accounts(payload):
    if isinstance(payload,list): return payload
    if isinstance(payload,dict):
        for key in ('data','accounts','result'):
            v=payload.get(key)
            if isinstance(v,list):return v
            if isinstance(v,dict):
                for k in ('accounts','data'):
                    if isinstance(v.get(k),list):return v[k]
    return []

def demo_id(accounts):
    for a in accounts:
        if not isinstance(a,dict):continue
        fields=(a.get(k) for k in ('account_type','type','account_category','account_kind','environment'))
        is_demo = a.get('is_virtual') is True or a.get('is_demo') is True or any(str(x).lower() in ('demo','virtual') for x in fields)
        if not is_demo:continue
        for k in ('account_id','loginid','id'):
            x=a.get(k)
            if isinstance(x,(str,int)) and str(x):return str(x)
    return None


def real_accounts(accounts):
    """Return only explicitly real Options accounts; never guess classification."""
    found=[]
    for a in accounts:
        if not isinstance(a,dict):continue
        fields=(a.get(k) for k in ('account_type','type','account_category','account_kind','environment'))
        recognized=any(str(x).lower()=='real' for x in fields if x is not None)
        if not recognized or a.get('is_virtual') is True or a.get('is_demo') is True:
            continue
        account_id=next((str(a[k]) for k in ('account_id','loginid','id') if isinstance(a.get(k),(str,int)) and str(a[k])),None)
        if account_id:
            found.append({'account_id':account_id,'account_type':'real',
                          'currency':str(a.get('currency') or ''),
                          'status':str(a.get('status') or 'unknown')})
    return found


def real_balance(accounts, account_id=None):
    """Read a proven-real account balance without guessing which account to use.

    The selected account_id, if given, must exactly match the OAuth account list.
    With multiple real accounts and no selection, return None instead of showing
    the balance of an arbitrary account. Missing data is never synthetic zero.
    """
    from decimal import Decimal, InvalidOperation
    verified=real_accounts(accounts)
    ids={entry['account_id'] for entry in verified}
    if account_id is not None:
        if not isinstance(account_id,str) or account_id not in ids:
            return None
        chosen=account_id
    elif len(ids)==1:
        chosen=next(iter(ids))
    else:
        return None
    for account in accounts:
        if not isinstance(account,dict):
            continue
        identified=real_accounts([account])
        if not identified or identified[0]['account_id']!=chosen:
            continue
        found=identified[0]
        raw=account.get('balance')
        if raw is None:
            return {**found,'balance':None,'available':False}
        try:
            amount=Decimal(str(raw))
        except (InvalidOperation,ValueError,TypeError):
            return {**found,'balance':None,'available':False}
        if not amount.is_finite() or amount<0:
            return {**found,'balance':None,'available':False}
        currency=found['currency']
        if not currency or not currency.isalnum() or len(currency)>20:
            return {**found,'balance':None,'available':False}
        return {**found,'balance':str(amount),'available':True}
    return None
