"""Cursor desktop authentication and quota APIs, built into Headroom.

Adapted from CodexBar's MIT protocol. Only the app's own auth database is read;
no browser cookie extraction, credential refresh or credential persistence.
"""
import base64
import json
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import quote, unquote
import urllib.error
from .analytics import iso, number, timestamp
from .provider_http import request_json

SOURCE='cursor_usage_api'
BASE='https://cursor.com'


def _subject(value):
    if not isinstance(value,str):return None
    value=value.strip().split('|')[-1]
    return value if re.fullmatch(r'[A-Za-z0-9._-]{1,256}',value) else None


def _session(token):
    if not isinstance(token,str) or len(token)>16384 or any(c.isspace() for c in token):raise PermissionError('Cursor sign-in required')
    parts=token.split('.')
    if len(parts)!=3:raise PermissionError('Cursor app credential required')
    try:
        claims=json.loads(base64.urlsafe_b64decode(parts[1]+'='*((-len(parts[1]))%4)))
        subject=_subject(claims.get('sub'))
        expires=number(claims.get('exp'))
    except (ValueError,TypeError,AttributeError):raise PermissionError('Cursor credential unavailable') from None
    if not subject or expires is None or expires<=time.time()+60:raise PermissionError('Cursor sign-in needs renewal')
    return 'WorkosCursorSessionToken='+subject+'%3A%3A'+token,subject


def _read_app_token():
    path=Path.home()/'Library/Application Support/Cursor/User/globalStorage/state.vscdb'
    if not path.is_file():raise PermissionError('Cursor app sign-in required')
    def read(immutable=False):
        uri=path.resolve().as_uri()+'?mode=ro'+('&immutable=1' if immutable else '')
        with sqlite3.connect(uri,uri=True,timeout=.25) as db:
            db.execute('PRAGMA query_only=ON')
            row=db.execute("SELECT value FROM ItemTable WHERE key=? LIMIT 1",('cursorAuth/accessToken',)).fetchone()
        value=row[0] if row else None
        # VS Code's ItemTable stores values as BLOBs. Swift's sqlite3_column_text
        # converts them to UTF-8; Python's sqlite3 returns bytes instead.
        if isinstance(value,bytes):
            if len(value)>16384:raise ValueError('Credential size limit')
            value=value.decode('utf-8')
        return value
    try:return read()
    except sqlite3.OperationalError:
        if Path(str(path)+'-wal').exists() or Path(str(path)+'-shm').exists():raise
        return read(True)


def _cookie(value):
    if not isinstance(value,str) or len(value)>32768 or '\r' in value or '\n' in value:raise ValueError('Invalid session')
    raw=None
    for component in value.split(';'):
        pair=component.strip().split('=',1)
        if len(pair)==2 and pair[0]=='WorkosCursorSessionToken':raw=unquote(pair[1]);break
    if raw is None:raw=unquote(value)
    pieces=raw.split('::')
    if len(pieces)!=2 or not _subject(pieces[0]) or not pieces[1]:raise ValueError('Invalid session')
    subject=_subject(pieces[0]);token=pieces[1]
    if any(c.isspace() for c in token) or ';' in token:raise ValueError('Invalid session')
    if token.count('.')==2:
        _,jwt_subject=_session(token)
        if jwt_subject!=subject:raise ValueError('Session account mismatch')
    return 'WorkosCursorSessionToken='+subject+'%3A%3A'+token,subject


def _existing_session():
    # Optional legacy migration file. It is never created, cleared or rewritten.
    candidates=[]
    try:
        path=Path.home()/'Library/Application Support/CodexBar/cursor-cookie.json'
        with path.open('rb') as stream:raw=stream.read(65537)
        if len(raw)<=65536:candidates.append(json.loads(raw).get('cookieHeader'))
    except (OSError,ValueError,AttributeError):pass
    for candidate in candidates:
        try:return _cookie(candidate)
        except (ValueError,PermissionError):continue
    raise PermissionError('Cursor app sign-in required')


def credentials(store):
    app_reason='app_auth_unavailable'
    try:
        cookie,subject=_session(_read_app_token())
        return cookie,subject,None
    except (OSError,ValueError,PermissionError,sqlite3.Error) as error:
        app_reason={'Cursor sign-in needs renewal':'app_auth_expired',
            'Cursor app sign-in required':'app_auth_missing',
            'Cursor app credential required':'app_auth_format',
            'Cursor credential unavailable':'app_auth_format',
            'Cursor sign-in required':'app_auth_missing'}.get(str(error),'app_auth_unavailable')
    # Optional compatibility with an already-configured manual session. It is
    # read in memory and never copied into Headroom or a browser cookie store.
    try:
        path=Path.home()/'.config/codexbar/config.json'
        with path.open('rb') as stream:raw=stream.read(1024*1024+1)
        if len(raw)>1024*1024:raise ValueError('Configuration size limit')
        config=json.loads(raw)
        provider=next(p for p in config.get('providers',[]) if isinstance(p,dict) and p.get('id')=='cursor')
        accounts=provider.get('tokenAccounts') or {}
        values=accounts.get('accounts') or [];index=accounts.get('activeIndex',0)
        if type(index) is not int or not isinstance(values,list) or not 0<=index<len(values):raise ValueError('Invalid selected session')
        account=values[index];cookie,subject=_cookie(account.get('token'))
        scope='|'.join(str(account.get(k,'')) for k in ('id','token','organizationId','workspaceID'))
        return cookie,subject,store.fingerprint('cursor|configured|'+scope)
    except (OSError,ValueError,TypeError,AttributeError,StopIteration,sqlite3.Error):
        try:
            cookie,subject=_existing_session()
            return cookie,subject,None
        except PermissionError:raise PermissionError(app_reason) from None


def _pct(value):
    value=number(value)
    return value if value is not None and value>=0 else None


def normalize(summary,user,store,subject,observed_at=None,legacy_ref=None,requests=None):
    if not isinstance(summary,dict) or not isinstance(user,dict):raise ValueError('Cursor response objects required')
    confirmed=_subject(user.get('sub'))
    if confirmed and confirmed!=subject:raise ValueError('Cursor account mismatch')
    stamp=observed_at or iso();end=timestamp(summary.get('billingCycleEnd'));start=timestamp(summary.get('billingCycleStart'))
    minutes=int((end-start).total_seconds()/60) if start and end and end>start else None
    if minutes is not None and not 0<minutes<=5270400:minutes=None
    individual=summary.get('individualUsage');individual=individual if isinstance(individual,dict) else {}
    plan=individual.get('plan');plan=plan if isinstance(plan,dict) else {}
    windows=[]
    def add(key,label,used):
        windows.append({'id':key,'label':label,'used_pct':float(used) if used is not None else None,
            'remaining_pct':float(max(0,100-used)) if used is not None else None,
            'resets_at':iso(end) if end else None,'window_minutes':minutes,
            'status':'fresh' if used is not None else 'unknown','observed_at':stamp,'source':SOURCE})
    def ratio(block):
        if not isinstance(block,dict) or block.get('enabled') is False:return None
        used,limit=number(block.get('used')),number(block.get('limit'))
        return used*100/limit if used is not None and used>=0 and limit is not None and limit>0 else None
    total=_pct(plan.get('totalPercentUsed')) if plan.get('enabled') is not False else None
    if total is None:total=ratio(plan)
    # Never average independent pool percentages into a made-up plan total.
    add('primary','Included plan',total)
    for key,label,field in [('secondary','Cursor models','autoPercentUsed'),('tertiary','Third-party models','apiPercentUsed')]:
        if field in plan:add(key,label,_pct(plan[field]) if plan.get('enabled') is not False else None)
    if not plan and isinstance(individual.get('overall'),dict):
        add('individual-overall','Individual cap',ratio(individual['overall']))
    team=summary.get('teamUsage')
    if not plan and not individual.get('overall') and isinstance(team,dict) and isinstance(team.get('pooled'),dict):
        add('team-pooled','Shared team pool',ratio(team['pooled']))
    if requests is not None and isinstance(requests,dict):
        block=requests.get('gpt4')
        if isinstance(block,dict):
            used=_pct(block.get('numRequestsTotal') if block.get('numRequestsTotal') is not None else block.get('numRequests'))
            limit=number(block.get('maxRequestUsage'))
            if used is not None and limit is not None and limit>0:
                add('legacy-requests','Legacy request allowance',used*100/limit)
    tier=summary.get('membershipType')
    if not isinstance(tier,str) or len(tier)>80 or '@' in tier or any(ord(c)<32 for c in tier):tier=None
    if tier:tier={'pro':'Cursor Pro','pro_plus':'Cursor Pro+','ultra':'Cursor Ultra','free':'Cursor Free'}.get(tier,tier)
    fresh=any(w['status']=='fresh' for w in windows)
    return {'id':'cursor','observed_at':stamp,'source':SOURCE,
        'status':'verified' if fresh and all(w['status']=='fresh' for w in windows) else 'partial',
        'verified_tier':tier,'auth_method':'session','auth_verified':True,
        'account_ref':legacy_ref or store.fingerprint('cursor|'+subject.lower()), 'windows':windows,'wallet':None,
        'error':None if fresh else {'code':'no_quota_data','message':'Cursor authenticated but did not report a measured allowance.'}}


def failure(code,message,status='error'):
    return {'id':'cursor','observed_at':iso(),'source':SOURCE,'status':status,'verified_tier':None,
        'auth_method':'session','auth_verified':False,'account_ref':None,'windows':[],'wallet':None,
        'error':{'code':code,'message':message}}


def collect(store):
    try:
        cookie,subject,legacy_ref=credentials(store);headers={'Cookie':cookie};sent=iso()
        summary=request_json(BASE+'/api/usage-summary',headers=headers,timeout=10)
        user=request_json(BASE+'/api/auth/me',headers=headers,timeout=6)
        requests=None
        plan=summary.get('individualUsage',{}).get('plan') if isinstance(summary,dict) and isinstance(summary.get('individualUsage',{}),dict) else None
        if not isinstance(plan,dict) or not any(k in plan for k in ('totalPercentUsed','used','autoPercentUsed','apiPercentUsed')):
            try:requests=request_json(BASE+'/api/usage?user='+quote(subject,safe=''),headers=headers,timeout=5)
            except (OSError,ValueError,TypeError):pass
        return normalize(summary,user,store,subject,sent,legacy_ref,requests)
    except PermissionError as error:
        code=str(error) if str(error) in ('app_auth_expired','app_auth_missing','app_auth_format','app_auth_unavailable') else 'auth_required'
        return failure(code,'Renew sign-in in the Cursor desktop app to connect usage.','auth_required')
    except urllib.error.HTTPError as error:
        if error.code in (401,403):return failure('auth_required','Cursor app sign-in needs renewal.','auth_required')
        if error.code==429:
            result=failure('rate_limited','Cursor usage API is rate limited; backing off.')
            delay=number(error.headers.get('Retry-After')) if error.headers else None
            result['retry_after_seconds']=float(max(60,min(3600,delay))) if delay is not None else 60
            return result
        return failure('provider_error','Cursor usage API is temporarily unavailable.')
    except (OSError,ValueError,TypeError,AttributeError,sqlite3.Error):
        return failure('collector_error','Cursor quota could not be verified; retrying automatically.')
