"""Strict normalization for the Muse Code subscription-quota portal feed.

API billing balances are intentionally excluded. Missing denominators remain unknown.
The optional browser bridge sends only quota fields, never session credentials.
"""
import json
from datetime import timedelta
from .analytics import iso, number, timestamp, utc_now
from .registry import MAX_DATA_AGE_SECONDS

SOURCE = 'muse_oauth_subscription_quota'
TIERS = {'Muse Code Maximum Usage', 'Muse Code Everyday', 'Muse Code Everyday Usage', 'Muse Code Expanded Usage'}
# Muse Code's shipped client uses this account-token endpoint and header version.
# It returns the existing plan credential plus quota. Only quota/tier/account HMAC
# cross this module's boundary; the returned key and profile stay in RAM.
ENDPOINT = 'https://api.meta.ai/muse-code/key'
import subprocess
import threading
import urllib.request
import urllib.error
from datetime import datetime, timezone

_read_lock = threading.Lock()
_keychain_blocked = False


def retry_credentials():
    global _keychain_blocked
    with _read_lock: _keychain_blocked = False


def credentials():
    global _keychain_blocked
    with _read_lock:
        if _keychain_blocked: raise PermissionError('Keychain approval required')
        try:
            response = subprocess.run(['/usr/bin/security','find-generic-password',
                '-s','ai.meta.dev.credentials','-a','meta','-w'],
                capture_output=True,timeout=5,check=False)
        except subprocess.TimeoutExpired:
            # Never accumulate background credential dialogs after access changes.
            _keychain_blocked = True
            raise PermissionError('Keychain approval required') from None
        if response.returncode != 0: raise PermissionError('Muse login required')
        if len(response.stdout)>65536: raise ValueError('Credential size limit')
        value = json.loads(response.stdout)
        token = value.get('access_token')
        if not isinstance(token,str) or not token or len(token)>16384 or any(c in token for c in '\r\n'):
            raise PermissionError('Muse OAuth required')
        return token


def fetch_subscription(token):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):return None
    request=urllib.request.Request(ENDPOINT,data=b'{}',method='POST',headers={
        'Authorization':'Bearer '+token,'x-api-version':'1.0.0',
        'Content-Type':'application/json','User-Agent':'Headroom/0.1'})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    with opener.open(request,timeout=12) as response:
        body=response.read(1024*1024+1)
        if len(body)>1024*1024:raise ValueError('Quota size limit')
        result=json.loads(body)
        if not isinstance(result,dict):raise ValueError('Object required')
        return result


def normalize_subscription(payload, store, observed_at=None):
    if not isinstance(payload,dict):raise ValueError('Object required')
    stamp=observed_at or iso()
    observed=timestamp(stamp)
    if not observed or observed>utc_now()+timedelta(seconds=30):raise ValueError('Invalid observation')
    identity=payload.get('user_email')
    if not isinstance(identity,str) or not identity.strip() or len(identity)>320:raise ValueError('Missing account identity')
    account=store.fingerprint('muse|oauth|'+identity.strip().lower())
    tier=payload.get('subs_tier_name')
    if not isinstance(tier,str) or len(tier)>80 or '@' in tier or any(ord(c)<32 for c in tier):tier=None
    raw=payload.get('subs_usage')
    raw=raw if isinstance(raw,dict) else {}
    active=payload.get('is_subs_active') is True
    windows=[]
    for key,label,minutes in (('window','5-hour',300),('weekly','Weekly',10080)):
        entry=raw.get(key)
        if not isinstance(entry,dict):continue
        used=number(entry.get('used_percent'))
        known=active and used is not None and used>=0
        if key=='window':
            minutes=entry.get('window_duration_mins')
            if type(minutes) is not int or not 0<minutes<=5270400: known=False;minutes=None
            label='5-hour' if minutes==300 else 'Current window'
        reset=entry.get('resets_at');reset_at=None
        if type(reset) in (int,float) and number(reset) is not None:
            try:reset_at=iso(datetime.fromtimestamp(reset,timezone.utc))
            except (OverflowError,ValueError,OSError):pass
        windows.append({'id':key+'-'+str(minutes)+'m','label':label,'window_minutes':minutes,
            'used_pct':float(used) if known else None,'remaining_pct':float(max(0,100-used)) if known else None,
            'resets_at':reset_at,'status':'fresh' if known else 'unknown','observed_at':iso(observed),
            'source':'muse_code_oauth'})
    known=len(windows)==2 and all(w['status']=='fresh' for w in windows)
    return {'id':'muse','observed_at':iso(observed),'source':'muse_code_oauth',
        'status':'verified' if active and known else 'partial','verified_tier':tier if active else None,
        'auth_method':'oauth','auth_verified':True,'account_ref':account,'windows':windows,'wallet':None,
        'error':None if known else {'code':'coding_quota_unavailable',
            'message':'Muse OAuth verified; an active coding allowance and valid quota fields are required.'}}


def collect(store):
    try:
        token=credentials()
        # The shipped protocol dates mint observations at request send, not completion.
        sent=iso()
        return normalize_subscription(fetch_subscription(token),store,sent)
    except PermissionError:
        code,message='muse_login_required','Run muse login, approve macOS credential access if requested, then Refresh now. Headroom never asks for your password.'
    except urllib.error.HTTPError as error:
        if error.code==429:
            return {'id':'muse','observed_at':iso(),'source':'muse_code_oauth','status':'error',
                'verified_tier':None,'auth_method':'oauth','auth_verified':False,'account_ref':None,'windows':[],
                'error':{'code':'rate_limited','message':'Muse quota endpoint is rate limited; backing off and withholding current values.'},
                'retry_after_seconds':60}
        code='muse_login_required' if error.code in (401,403) else 'provider_error'
        message='Muse OAuth login needs renewal; run muse login, then Refresh now.' if code=='muse_login_required' else 'Muse quota endpoint is temporarily unavailable; retrying automatically.'
    except (OSError,ValueError,TypeError,AttributeError,subprocess.TimeoutExpired):
        code,message='collector_error','Muse coding quota could not be verified; retrying automatically.'
    proof={}
    try:
        proof=json.loads((store.root/'muse-verification.json').read_text())
        if not isinstance(proof,dict):proof={}
    except (OSError,ValueError):pass
    return {'id':'muse','observed_at':proof.get('observed_at'),'source':'muse_code_oauth',
        'status':'auth_required' if code=='muse_login_required' else 'error',
        'verified_tier':proof.get('verified_tier') if proof.get('verified_tier') in TIERS else None,
        'auth_method':'oauth','auth_verified':False,'account_ref':proof.get('account_ref'),
        'windows':[],'wallet':None,'error':{'code':code,'message':message}}
