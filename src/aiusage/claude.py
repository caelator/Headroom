"""Read-only Claude OAuth quotas. Credential and profile bytes live only in RAM."""
import json
import math
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import timezone
from email.utils import parsedate_to_datetime
from .analytics import iso, number, timestamp, utc_now, plausible_reset

API = 'https://api.anthropic.com/api/oauth/'
LIMIT = 1024 * 1024
PROFILE_TTL_SECONDS = 3600
PROFILE_STALE_SECONDS = 86400
RATE_LIMIT_MIN_SECONDS = 60
RATE_LIMIT_MAX_SECONDS = 3600
_profile_lock = threading.Lock()
_credential_lock = threading.Lock()
_rate_limit_lock = threading.Lock()
_keychain_blocked = False
_profile_cache = {'credential_ref':None,'data':None,'until':0,'stale_until':0,'observed_at':None}
_rate_limit = {'credential_ref':None,'until':0}
WINDOWS = {'five_hour': ('5-hour', 300), 'seven_day': ('Weekly', 10080),
           'seven_day_sonnet': ('Sonnet weekly', 10080), 'seven_day_opus': ('Opus weekly', 10080),
           'seven_day_oauth_apps': ('OAuth apps weekly', 10080), 'seven_day_cowork': ('Cowork weekly', 10080)}


def retry_credentials():
    global _keychain_blocked
    with _credential_lock:_keychain_blocked=False


def clear_rate_limit():
    with _rate_limit_lock:_rate_limit.update(credential_ref=None,until=0)


def forget_profile():
    with _profile_lock:
        _profile_cache.update(credential_ref=None,data=None,until=0,stale_until=0,observed_at=None)


def credentials():
    global _keychain_blocked
    with _credential_lock:
        if _keychain_blocked:raise PermissionError('Claude credential approval required')
        try:
            result=subprocess.run(['/usr/bin/security','find-generic-password','-s','Claude Code-credentials','-w'],
                capture_output=True,timeout=5,check=False)
        except subprocess.TimeoutExpired:
            _keychain_blocked=True;raise PermissionError('Claude credential approval required') from None
        if result.returncode or len(result.stdout)>65536:raise PermissionError('Claude OAuth unavailable')
        raw=json.loads(result.stdout).get('claudeAiOauth') or {}
        token=raw.get('accessToken')
        if not isinstance(token,str) or not token or len(token)>16384 or any(c in token for c in '\r\n'):
            raise PermissionError('Claude OAuth unavailable')
        return token


def failure(code, message):
    return {'id': 'claude', 'observed_at': iso(), 'source': 'claude_oauth_usage',
            'status': 'auth_required' if code == 'auth_required' else 'error',
            'verified_tier': None, 'auth_method': 'oauth', 'auth_verified': False,
            'account_ref': None, 'windows': [], 'wallet': None,
            'error': {'code': code, 'message': message}}


def parse_retry_after(headers, now=None):
    """Return non-negative Retry-After seconds for delta-seconds or HTTP-date, else None."""
    try:
        raw = headers.get('Retry-After') if headers is not None else None
    except (AttributeError, TypeError, ValueError):
        return None
    if raw is None or isinstance(raw, bool): return None
    text = str(raw).strip()
    if not text or len(text) > 128: return None
    parsed_number = number(text)
    if parsed_number is not None:
        value = float(parsed_number)
        return max(0.0, value) if math.isfinite(value) else None
    try:
        parsed_date = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed_date is None: return None
    if parsed_date.tzinfo is None: parsed_date = parsed_date.replace(tzinfo=timezone.utc)
    delta = (parsed_date.astimezone(timezone.utc) - (now or utc_now()).astimezone(timezone.utc)).total_seconds()
    return max(0.0, delta) if math.isfinite(delta) else None


def bounded_retry_delay(headers=None):
    parsed = parse_retry_after(headers)
    delay = parsed if parsed is not None else RATE_LIMIT_MIN_SECONDS
    return float(max(RATE_LIMIT_MIN_SECONDS, min(RATE_LIMIT_MAX_SECONDS, delay)))


def _rate_limited_failure(credential_ref, headers=None):
    delay = bounded_retry_delay(headers)
    with _rate_limit_lock:
        _rate_limit.update(credential_ref=credential_ref, until=time.monotonic() + delay)
    result = failure('rate_limited', 'Claude quota API is rate limited; current percentages are withheld while retrying with backoff.')
    result['retry_after_seconds'] = delay
    return result


def _active_rate_limit(credential_ref):
    with _rate_limit_lock:
        if _rate_limit.get('credential_ref') != credential_ref: return None
        remaining = _rate_limit.get('until', 0) - time.monotonic()
    if remaining <= 0:
        clear_rate_limit(); return None
    result = failure('rate_limited', 'Claude quota API is rate limited; current percentages are withheld while retrying with backoff.')
    result['retry_after_seconds'] = float(math.ceil(remaining))
    return result


def read_json(path, token):
    request = urllib.request.Request(API + path, headers={'Authorization': 'Bearer ' + token,
        'Accept': 'application/json',
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


def _load_profile(token, credential_ref):
    """Return (profile, tier_verified_at).

    A fresh cached profile avoids a profile request. An expired cached profile
    may be reused only for the exact same credential fingerprint and only
    within its stale bound when the profile endpoint is rate limited or has a
    transient server failure. This keeps valid usage attributable to an
    already verified account without inventing identity for a new credential.
    """
    now = time.monotonic()
    with _profile_lock:
        cached = dict(_profile_cache)
    if (cached.get('credential_ref') == credential_ref and isinstance(cached.get('data'), dict)
            and cached.get('until', 0) > now):
        return cached['data'], cached.get('observed_at')
    try:
        data = read_json('profile', token)
    except urllib.error.HTTPError as error:
        stale_usable = (cached.get('credential_ref') == credential_ref and isinstance(cached.get('data'), dict)
            and cached.get('stale_until', 0) > now)
        if stale_usable and (error.code == 429 or error.code >= 500):
            with _profile_lock:
                _profile_cache['until'] = min(cached['stale_until'], now + bounded_retry_delay(error.headers))
            return cached['data'], cached.get('observed_at')
        raise
    except (OSError, ValueError, TypeError, AttributeError):
        stale_usable = (cached.get('credential_ref') == credential_ref and isinstance(cached.get('data'), dict)
            and cached.get('stale_until', 0) > now)
        if stale_usable:
            with _profile_lock:
                _profile_cache['until'] = min(cached['stale_until'], now + RATE_LIMIT_MIN_SECONDS)
            return cached['data'], cached.get('observed_at')
        raise
    observed_at = iso()
    with _profile_lock:
        _profile_cache.update(credential_ref=credential_ref, data=data,
            until=now + PROFILE_TTL_SECONDS, stale_until=now + PROFILE_STALE_SECONDS,
            observed_at=observed_at)
    return data, observed_at


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
            'resets_at': plausible_reset(iso(reset),stamp,minutes) if reset else None, 'window_minutes': minutes,
            'status': 'fresh' if known else 'unknown', 'observed_at': stamp, 'source': 'claude_oauth_usage'})
    return {'id': 'claude', 'observed_at': stamp, 'source': 'claude_oauth_usage',
        'status': 'verified' if windows and all(w['status']=='fresh' for w in windows) else 'partial',
        'verified_tier': tier, 'auth_method': 'oauth', 'auth_verified': True,
        'account_ref': ref, 'windows': windows, 'wallet': None,
        'error': None if windows else {'code':'no_quota_data','message':'Claude authenticated but reported no quota windows.'}}


def collect(store):
    credential_ref = None
    try:
        token=credentials();sent=iso()
        credential_ref=store.fingerprint('claude|profile-cache|'+token)
        blocked=_active_rate_limit(credential_ref)
        if blocked is not None:return blocked
        # Usage first: a usage 429 must not spend a profile request, and a
        # profile 429 must not be reached at all when usage is unavailable.
        usage = read_json('usage', token)
        try:
            profile, tier_verified_at = _load_profile(token, credential_ref)
        except urllib.error.HTTPError as error:
            if error.code == 429:
                # No verified account is available for this credential (a
                # usable cached profile is already handled in _load_profile),
                # so the valid-looking usage payload cannot be attributed
                # safely and is withheld rather than silently trusted.
                return _rate_limited_failure(credential_ref, error.headers)
            raise
        result=normalize(usage,profile,store,sent)
        result['tier_verified_at']=tier_verified_at
        clear_rate_limit()
        return result
    except PermissionError:
        forget_profile()
        return failure('auth_required','Sign in to Claude Code, approve normal credential access if requested, then Refresh now.')
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            forget_profile()
            return failure('auth_required', 'Claude OAuth sign-in needs renewal in Claude Code.')
        if error.code==429:
            return _rate_limited_failure(credential_ref, error.headers if hasattr(error, 'headers') else None)
        return failure('provider_error', 'Claude usage API is temporarily unavailable; retrying automatically.')
    except (OSError, ValueError, TypeError, AttributeError, subprocess.TimeoutExpired):
        return failure('collector_error', 'Claude usage could not be verified; retrying automatically.')
