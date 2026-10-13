"""Built-in z.ai Coding Plan quotas; adapted from CodexBar's MIT protocol."""
import json
import os
from pathlib import Path
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit
import urllib.error
from .analytics import iso, number, utc_now
from .provider_http import request_json

SOURCE = 'zai_coding_api'
REGIONS = {'global': 'https://api.z.ai', 'bigmodel-cn': 'https://open.bigmodel.cn'}
QUOTA_PATH = '/api/monitor/usage/quota/limit'


def _json(path):
    with path.open('rb') as stream: raw=stream.read(1024*1024+1)
    if len(raw)>1024*1024:raise ValueError('Configuration size limit')
    result=json.loads(raw)
    return result if isinstance(result,dict) else {}


def _token(value):
    return value.strip() if isinstance(value,str) and value.strip() and len(value)<=16384 and not any(c.isspace() for c in value.strip()) else None


def credentials():
    region=os.environ.get('HEADROOM_ZAI_REGION',os.environ.get('Z_AI_REGION'))
    key=_token(os.environ.get('Z_AI_API_KEY') or os.environ.get('ZAI_API_KEY'))
    legacy=None
    try:
        config=_json(Path.home()/'.config/codexbar/config.json')
        legacy=next((p for p in config.get('providers',[]) if isinstance(p,dict) and p.get('id')=='zai'),None)
    except (OSError,ValueError,TypeError):pass
    if region is None:region=(legacy or {}).get('region','global')
    if region not in REGIONS:raise ValueError('Unsupported z.ai region')
    organization=project=None
    if key is None and legacy:
        accounts=legacy.get('tokenAccounts')
        if isinstance(accounts,dict) and accounts.get('accounts'):
            values=accounts['accounts'];index=accounts.get('activeIndex',0)
            if not isinstance(values,list) or type(index) is not int or not 0<=index<len(values):
                raise ValueError('Invalid active account')
            account=values[index]
            if not isinstance(account,dict):raise ValueError('Invalid active account')
            key=_token(account.get('token'))
            organization=account.get('organizationID') or account.get('organizationId')
            project=account.get('workspaceID')
            if account.get('usageScope')=='team':raise ValueError('Team quota requires separate integration')
        else:key=_token(legacy.get('apiKey'))
    if key is None and region=='bigmodel-cn':
        for name in ('BIGMODEL_API_KEY','ZHIPU_API_KEY','ZHIPUAI_API_KEY','GLM_API_KEY'):
            key=_token(os.environ.get(name))
            if key:break
        if key is None:
            for relative in ('.coding-relay/glm-api-key','.config/bigmodel/api_key','.config/zhipu/api_key'):
                try:
                    with (Path.home()/relative).open() as stream:key=_token(stream.readline(16385))
                except OSError:continue
                if key:break
    if key is None:
        try:
            env=_json(Path.home()/'.claude/settings.json').get('env') or {}
            base=urlsplit(env.get('ANTHROPIC_BASE_URL',''))
            if base.scheme=='https' and base.hostname==urlsplit(REGIONS[region]).hostname and not base.username and base.port in (None,443):
                key=_token(env.get('ANTHROPIC_AUTH_TOKEN') or env.get('ANTHROPIC_API_KEY'))
        except (OSError,ValueError,TypeError,AttributeError):pass
    if not key:raise PermissionError('Coding-plan API key required')
    if os.environ.get('Z_AI_USAGE_SCOPE','personal')!='personal':raise ValueError('Unsupported quota scope')
    endpoint=REGIONS[region]+QUOTA_PATH
    for name,expected in [('Z_AI_API_HOST',REGIONS[region]),('Z_AI_QUOTA_URL',endpoint),('Z_AI_QUOTA_ENDPOINT',endpoint)]:
        if os.environ.get(name) and os.environ[name].rstrip('/')!=expected:
            raise ValueError('Unsupported endpoint override')
    return key,region,organization,project


def normalize(payload,store,key,region,observed_at=None,organization=None,project=None):
    if not isinstance(payload,dict) or payload.get('success') is not True or type(payload.get('code')) is not int or payload['code']!=200:
        raise ValueError('Unsuccessful quota response')
    data=payload.get('data')
    if not isinstance(data,dict) or not isinstance(data.get('limits'),list):raise ValueError('Quota limits required')
    stamp=observed_at or iso();windows=[];seen=set();incomplete=False
    for raw in data['limits'][:32]:
        if not isinstance(raw,dict) or raw.get('type') not in ('TOKENS_LIMIT','CREDIT_LIMIT','TIME_LIMIT'):
            incomplete=True;continue
        unit,count=raw.get('unit'),raw.get('number')
        minutes=count*{1:1440,3:60,5:1,6:10080}.get(unit,0) if type(unit) is int and type(count) is int and count>0 else None
        if not minutes or minutes>5270400:minutes=None
        kind=raw['type']
        # TIME_LIMIT unit 5 / number 1 is the provider's monthly MCP marker,
        # not a one-minute rolling allowance. Its duration stays unknown.
        if kind=='TIME_LIMIT' and unit==5 and count==1:minutes=None
        key_id='zai-mcp' if kind=='TIME_LIMIT' else 'primary' if minutes==300 else 'secondary' if minutes==10080 else 'quota-'+kind.lower()+'-'+str(minutes)
        if key_id in seen:incomplete=True;continue
        seen.add(key_id)
        used=number(raw.get('percentage'))
        if used is not None and used<0:used=None
        allowance,current,remaining=(number(raw.get(k)) for k in ('usage','currentValue','remaining'))
        if allowance is not None and allowance>0:
            observations=[]
            if current is not None and current>=0:observations.append(current)
            if remaining is not None and 0<=remaining<=allowance:observations.append(allowance-remaining)
            if observations:used=max(observations)*100/allowance
        known=used is not None
        reset=None;epoch=number(raw.get('nextResetTime'))
        if epoch is not None and epoch>0:
            try:
                reset=datetime.fromtimestamp(float(epoch/1000),timezone.utc)
                if minutes==300 and reset>utc_now()+timedelta(hours=5,minutes=1):reset=None
            except (ValueError,OverflowError,OSError):pass
        label='MCP' if kind=='TIME_LIMIT' else '5-hour' if minutes==300 else 'Weekly' if minutes==10080 else 'Coding quota'
        windows.append({'id':key_id,'label':label,'used_pct':float(used) if known else None,
            'remaining_pct':float(max(0,100-used)) if known else None,'resets_at':iso(reset) if reset else None,
            'window_minutes':minutes,'status':'fresh' if known else 'unknown','observed_at':stamp,'source':SOURCE})
    tier=next((data.get(k) for k in ('planName','plan','plan_type','packageName','level')
               if isinstance(data.get(k),str) and data[k].strip() and len(data[k])<=80
               and '@' not in data[k] and not any(ord(c)<32 for c in data[k])),None)
    scope='zai|configured|'+region+'|'+key
    if organization or project:scope+='|'+str(organization or '')+'|'+str(project or '')
    return {'id':'zai','observed_at':stamp,'source':SOURCE,
        'status':'verified' if windows and not incomplete and all(w['status']=='fresh' for w in windows) else 'partial',
        'verified_tier':tier.strip() if tier else None,'auth_method':'api_key','auth_verified':True,
        'account_ref':store.fingerprint(scope),'windows':windows,'wallet':None,
        'error':None if windows else {'code':'no_quota_data','message':'z.ai reported no coding quota limits.'}}


def failure(code,message,status='error'):
    return {'id':'zai','observed_at':iso(),'source':SOURCE,'status':status,'verified_tier':None,
        'auth_method':'api_key','auth_verified':False,'account_ref':None,'windows':[],'wallet':None,
        'error':{'code':code,'message':message}}


def collect(store):
    try:
        key,region,organization,project=credentials();sent=iso()
        payload=request_json(REGIONS[region]+QUOTA_PATH,headers={'Authorization':'Bearer '+key})
        return normalize(payload,store,key,region,sent,organization,project)
    except PermissionError:
        return failure('auth_required','Configure a z.ai coding-plan API key in the launch environment or supported coding client.','auth_required')
    except urllib.error.HTTPError as error:
        if error.code in (401,403):return failure('auth_required','z.ai coding-plan authentication needs renewal.','auth_required')
        if error.code==429:
            result=failure('rate_limited','z.ai quota API is rate limited; backing off.')
            delay=number(error.headers.get('Retry-After')) if error.headers else None
            result['retry_after_seconds']=float(max(60,min(3600,delay))) if delay is not None else 60
            return result
        return failure('provider_error','z.ai quota API is temporarily unavailable.')
    except (OSError,ValueError,TypeError,AttributeError):
        return failure('collector_error','z.ai quota could not be verified; check the configured region and coding-plan key.')
