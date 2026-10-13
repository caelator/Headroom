"""Read-only Codex OAuth quota collection, adapted from CodexBar's MIT protocol."""
import base64
import json
import os
from pathlib import Path
import re
import time
from datetime import datetime, timezone
import urllib.error
from .analytics import iso, number,plausible_reset
from .provider_http import request_json

SOURCE='codex_oauth_usage'
ENDPOINT='https://chatgpt.com/backend-api/wham/usage'


def _text(value,limit=160):
    return value.strip() if isinstance(value,str) and value.strip() and len(value)<=limit and '@' not in value and not any(ord(c)<32 for c in value) else None


def _claims(token):
    try:
        part=token.split('.')[1]
        value=json.loads(base64.urlsafe_b64decode(part+'='*((-len(part))%4)))
        return value if isinstance(value,dict) else {}
    except (ValueError,IndexError,TypeError):return {}


def credentials():
    root=Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex'))).expanduser()
    try:
        with (root/'auth.json').open('rb') as stream:raw=stream.read(1024*1024+1)
        if len(raw)>1024*1024:raise ValueError('Credential size limit')
        value=json.loads(raw);tokens=value.get('tokens')
        if not isinstance(tokens,dict):raise ValueError('OAuth credentials required')
        token=tokens.get('access_token')
        if not isinstance(token,str) or not token or len(token)>16384 or any(c.isspace() for c in token):raise ValueError('OAuth token required')
        claims=_claims(token);expiry=number(claims.get('exp'))
        if expiry is not None and expiry<=time.time():raise ValueError('OAuth needs renewal')
        account=_text(tokens.get('account_id'),256)
        if account is None:
            auth=_claims(tokens.get('id_token') or token).get('https://api.openai.com/auth')
            if isinstance(auth,dict):account=_text(auth.get('chatgpt_account_id'),256)
        if account is not None and not re.fullmatch(r'[A-Za-z0-9_.-]+',account):raise ValueError('Invalid account scope')
        return token,account
    except (OSError,ValueError,TypeError,AttributeError):
        raise PermissionError('Codex OAuth sign-in required') from None


def _reset(value):
    epoch=number(value)
    if epoch is None or epoch<=0:return None
    try:return iso(datetime.fromtimestamp(float(epoch),timezone.utc))
    except (ValueError,OverflowError,OSError):return None


def _window(raw,key,label,stamp):
    used=number(raw.get('used_percent')) if isinstance(raw,dict) else None
    if used is not None and used<0:used=None
    seconds=raw.get('limit_window_seconds') if isinstance(raw,dict) else None
    minutes=seconds//60 if type(seconds) is int and 60<=seconds<=5270400*60 else None
    return {'id':key,'label':label,'used_pct':float(used) if used is not None else None,
        'remaining_pct':float(max(0,100-used)) if used is not None else None,
        'resets_at':plausible_reset(_reset(raw.get('reset_at')),stamp,minutes) if isinstance(raw,dict) else None,'window_minutes':minutes,
        'status':'fresh' if used is not None else 'unknown','observed_at':stamp,'source':SOURCE}


def normalize(payload,store,account,observed_at=None):
    if not isinstance(payload,dict):raise ValueError('Quota object required')
    reported=_text(payload.get('account_id') or payload.get('accountId'),256)
    if reported and account and reported!=account:raise ValueError('Codex account mismatch')
    account=reported or account
    stamp=observed_at or iso();windows=[];seen=set();incomplete=False
    limits=payload.get('rate_limit')
    if limits is not None and not isinstance(limits,dict):incomplete=True
    if isinstance(limits,dict):
        for field,default in [('primary_window','primary'),('secondary_window','secondary')]:
            raw=limits.get(field)
            if raw is None:continue
            window=_window(raw,default,'Current window',stamp)
            if window['window_minutes']==10080:window.update(id='secondary',label='Weekly')
            elif window['window_minutes']==300:window.update(id='primary',label='5-hour')
            else:window['id']='codex-'+field+'-'+str(window['window_minutes'])
            if window['id'] in seen:incomplete=True;continue
            seen.add(window['id']);windows.append(window)
    extra=payload.get('additional_rate_limits')
    if extra is not None and not isinstance(extra,list):incomplete=True;extra=[]
    for entry in (extra or [])[:16]:
        if not isinstance(entry,dict):incomplete=True;continue
        name=_text(entry.get('limit_name'));metered=_text(entry.get('metered_feature'))
        label=name or metered or 'Model-specific quota'
        spark=any('spark' in v.lower() for v in [name,metered] if v)
        block=entry.get('rate_limit')
        if not isinstance(block,dict):incomplete=True;continue
        for field in ('primary_window','secondary_window'):
            raw=block.get(field)
            if raw is None:continue
            window=_window(raw,'','',stamp)
            if spark:
                weekly=window['window_minutes']==10080 or (window['window_minutes'] is None and field=='secondary_window')
                key='codex-spark-weekly' if weekly else 'codex-spark'
                title='Codex Spark Weekly' if weekly else 'Codex Spark 5-hour'
            else:
                slug=re.sub(r'[^a-z0-9]+','-',(metered or name or '').lower()).strip('-')
                if not slug:incomplete=True;continue
                key='codex-'+slug+('-weekly' if field=='secondary_window' else '')
                title=label+(' Weekly' if field=='secondary_window' else '')
            if key in seen:incomplete=True;continue
            seen.add(key);window.update(id=key,label=title);windows.append(window)
    # An explicit same-unit individual cap is separate from the API credit wallet.
    cap=payload.get('individual_limit') or (limits.get('individual_limit') if isinstance(limits,dict) else None)
    spend=payload.get('spend_control') or payload.get('spendControl')
    if cap is None and isinstance(spend,dict):cap=spend.get('individual_limit') or spend.get('individualLimit')
    if isinstance(cap,dict):
        remaining=number(cap.get('remaining_percent',cap.get('remainingPercent')))
        used=None
        if remaining is not None and 0<=remaining<=100:used=100-remaining
        if used is None:
            count,allowance=number(cap.get('used')),number(cap.get('limit'))
            if count is not None and count>=0 and allowance is not None and allowance>0:used=count*100/allowance
        window=_window({'used_percent':float(used) if used is not None else None,
            'reset_at':cap.get('reset_at',cap.get('resets_at',cap.get('resetsAt')))},'codex-individual-limit','Individual allowance',stamp)
        windows.append(window)
    tier=_text(payload.get('plan_type'),80)
    wallet=None;credits=payload.get('credits')
    if isinstance(credits,dict):
        balance=number(credits.get('balance'))
        if balance is not None and balance>=0:wallet={'balance':str(balance),'unit':'credits','label':'API credit balance'}
    fresh=any(w['status']=='fresh' for w in windows)
    return {'id':'codex','observed_at':stamp,'source':SOURCE,
        'status':'verified' if fresh and account and not incomplete and all(w['status']=='fresh' for w in windows) else 'partial',
        'verified_tier':tier,'auth_method':'oauth','auth_verified':True,
        'account_ref':store.fingerprint('codex|'+account) if account else None,'windows':windows[:32],
        'wallet':wallet,'error':None if fresh else {'code':'no_quota_data','message':'Codex reported no measured coding allowance.'}}


def failure(code,message,status='error'):
    return {'id':'codex','observed_at':iso(),'source':SOURCE,'status':status,'verified_tier':None,
        'auth_method':'oauth','auth_verified':False,'account_ref':None,'windows':[],'wallet':None,
        'error':{'code':code,'message':message}}


def collect(store):
    try:
        token,account=credentials();headers={'Authorization':'Bearer '+token}
        if account:headers['ChatGPT-Account-Id']=account
        sent=iso();payload=request_json(ENDPOINT,headers=headers)
        return normalize(payload,store,account,sent)
    except PermissionError:return failure('auth_required','Sign in to Codex or renew its OAuth login to connect subscription quotas.','auth_required')
    except urllib.error.HTTPError as error:
        if error.code in (401,403):return failure('auth_required','Codex OAuth sign-in needs renewal.','auth_required')
        if error.code==429:
            result=failure('rate_limited','Codex quota API is rate limited; backing off.')
            delay=number(error.headers.get('Retry-After')) if error.headers else None
            result['retry_after_seconds']=float(max(60,min(3600,delay))) if delay is not None else 60
            return result
        return failure('provider_error','Codex quota API is temporarily unavailable.')
    except (OSError,ValueError,TypeError,AttributeError):return failure('collector_error','Codex quota could not be verified; retrying automatically.')
