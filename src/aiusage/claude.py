"""Read-only Claude OAuth quotas. Credential and profile bytes live only in RAM."""
import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
from .analytics import iso, number, timestamp

API = 'https://api.anthropic.com/api/oauth/'
LIMIT = 1024 * 1024
_profile_lock = threading.Lock()
_profile_cache = {'token':None,'data':None,'until':0,'observed_at':None}
WINDOWS = {'five_hour': ('5-hour', 300), 'seven_day': ('Weekly', 10080),
           'seven_day_sonnet': ('Sonnet weekly', 10080), 'seven_day_opus': ('Opus weekly', 10080),
           'seven_day_oauth_apps': ('OAuth apps weekly', 10080), 'seven_day_cowork': ('Cowork weekly', 10080)}


def failure(code, message):
    return {'id': 'claude', 'observed_at': iso(), 'source': 'claude_oauth_usage',
            'status': 'auth_required' if code == 'auth_required' else 'error',
            'verified_tier': None, 'auth_method': 'oauth', 'auth_verified': False,
            'account_ref': None, 'windows': [], 'wallet': None,
            'error': {'code': code, 'message': message}}


def read_json(path, token):
    request = urllib.request.Request(API + path, headers={'Authorization': 'Bearer ' + token,
        'anthropic-beta': 'oauth-2025-04-20', 'User-Agent': 'Headroom/0.1'})
    # Avoid environment proxies, cookies and redirects to other credential recipients.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs): return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=12) as response:
        raw = response.read(LIMIT + 1)
        if len(raw) > LIMIT: raise ValueError('Response too large')
        data = json.loads(raw)
        if not isinstance(data, dict): raise ValueError('Object required')
        return data


def normalize(usage, profile, store, observed_at=None):
    stamp = observed_at or iso()
    account, organization = profile.get('account') or {}, profile.get('organization') or {}
    if not isinstance(account, dict) or not isinstance(organization, dict): raise ValueError('Invalid profile')
    uid, org = account.get('uuid'), organization.get('uuid')
    if not isinstance(uid, str) or not uid: raise ValueError('Missing account identity')
    ref = store.fingerprint('claude|' + uid + '|' + (org if isinstance(org, str) else ''))
    tier = 'Max' if account.get('has_claude_max') is True else 'Pro' if account.get('has_claude_pro') is True else None
    rate_tier = organization.get('rate_limit_tier')
    if tier == 'Max' and rate_tier in ('default_claude_max_20x', 'default_claude_max_5x'):
        tier += ' 20×' if rate_tier.endswith('20x') else ' 5×'
    windows = []
    for key, (label, minutes) in WINDOWS.items():
        raw = usage.get(key)
        if not isinstance(raw, dict): continue
        used = number(raw.get('utilization'))
        known = used is not None and used >= 0
        reset = timestamp(raw.get('resets_at'))
        windows.append({'id': key, 'label': label, 'used_pct': float(used) if known else None,
            'remaining_pct': float(max(0, 100-used)) if known else None,
            'resets_at': iso(reset) if reset else None, 'window_minutes': minutes,
            'status': 'fresh' if known else 'unknown', 'observed_at': stamp, 'source': 'claude_oauth_usage'})
    return {'id': 'claude', 'observed_at': stamp, 'source': 'claude_oauth_usage',
        'status': 'verified' if windows and all(w['status']=='fresh' for w in windows) else 'partial',
        'verified_tier': tier, 'auth_method': 'oauth', 'auth_verified': True,
        'account_ref': ref, 'windows': windows, 'wallet': None,
        'error': None if windows else {'code':'no_quota_data','message':'Claude authenticated but reported no quota windows.'}}


def collect(store):
    try:
        result = subprocess.run(['/usr/bin/security', 'find-generic-password', '-s', 'Claude Code-credentials', '-w'],
            capture_output=True, timeout=5, check=False)
        if result.returncode != 0: return failure('auth_required', 'Sign in to Claude Code to connect OAuth usage.')
        credentials = json.loads(result.stdout).get('claudeAiOauth') or {}
        token = credentials.get('accessToken')
        if not isinstance(token, str) or not token: return failure('auth_required', 'Claude OAuth credentials are unavailable.')
        usage = read_json('usage', token)
        with _profile_lock:
            if _profile_cache['token'] != token or _profile_cache['until'] < time.monotonic():
                _profile_cache.update(token=token,data=read_json('profile',token),until=time.monotonic()+300,observed_at=iso())
            profile = _profile_cache['data']
        result=normalize(usage,profile,store)
        result['tier_verified_at']=_profile_cache['observed_at']
        return result
    except urllib.error.HTTPError as error:
        if error.code in (401, 403): return failure('auth_required', 'Claude OAuth sign-in needs renewal in Claude Code.')
        if error.code==429:
            result=failure('rate_limited','Claude quota API is rate limited; current percentages are withheld while retrying with backoff.')
            try:delay=float(error.headers.get('Retry-After','60'))
            except (TypeError,ValueError):delay=60
            result['retry_after_seconds']=max(60,min(3600,delay))
            return result
        return failure('provider_error', 'Claude usage API is temporarily unavailable; retrying automatically.')
    except (OSError, ValueError, TypeError, AttributeError, subprocess.TimeoutExpired):
        return failure('collector_error', 'Claude usage could not be verified; retrying automatically.')
