"""Read the signed-in Antigravity app's process-owned loopback quota endpoint.

Protocol adapted from CodexBar; see THIRD_PARTY_NOTICES.md. Credentials and
process command lines remain in memory. No app, CLI or inference is launched.
"""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from .analytics import iso, number, timestamp
from .provider_http import NoRedirect

SERVICE = '/exa.language_server_pb.LanguageServerService/'
METHODS = frozenset({'GetUserStatus', 'RetrieveUserQuotaSummary'})
SOURCE = 'antigravity_local_app'
LIMIT = 1024 * 1024
BODY = {'metadata': {'ideName': 'antigravity', 'extensionName': 'antigravity',
                     'ideVersion': 'unknown', 'locale': 'en'}}


@dataclass(frozen=True)
class Endpoint:
    pid: int
    port: int
    scheme: str
    token: str = field(repr=False)


def _process(argv, timeout=3):
    result = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
    if result.returncode or len(result.stdout) > 8 * 1024 * 1024:
        raise OSError('Local app discovery unavailable')
    return result.stdout.decode(errors='replace')


def _app_path(path):
    value = path.lower()
    roots = ('/applications/', str(Path.home() / 'Applications').lower() + '/')
    return (value.startswith(roots)
            and any(name in value for name in ('/antigravity.app/contents/',
                '/antigravity ide.app/contents/', '/gemini.app/contents/'))
            and re.fullmatch(r'language[_-]server(?:[_-][a-z0-9]+)*', Path(value).name) is not None)


def _flag(command, name):
    match = re.search(re.escape(name) + r'(?:=|\s+)([^\s]+)', command)
    value = match[1].strip('"\'') if match else None
    return value if value and len(value) <= 4096 and not any(ord(c) < 32 for c in value) else None


def discover(deadline=None):
    deadline = deadline or time.monotonic() + 9
    def run(argv):
        remaining = deadline-time.monotonic()
        if remaining <= 0: raise TimeoutError('Local discovery deadline')
        return _process(argv, min(3,remaining))
    listing = run(['/bin/ps', '-ww', '-axo', 'uid=,pid=,comm='])
    endpoints = []
    for line in listing.splitlines():
        if time.monotonic() >= deadline: break
        parts = line.strip().split(None, 2)
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        if int(parts[0]) != os.getuid() or not _app_path(parts[2]):
            continue
        pid = int(parts[1])
        try: command = run(['/bin/ps', '-ww', '-p', str(pid), '-o', 'args='])
        except (OSError,subprocess.TimeoutExpired): continue
        token = _flag(command, '--csrf_token')
        if not token:
            continue
        try: sockets = run(['/usr/sbin/lsof', '-a', '-nP', '-p', str(pid), '-iTCP', '-sTCP:LISTEN', '-Fn'])
        except (OSError,subprocess.TimeoutExpired): continue
        if f'p{pid}' not in sockets.splitlines():
            continue
        ports = sorted({int(m[1]) for row in sockets.splitlines()
                        if (m := re.fullmatch(r'n[^\n]+:(\d+)', row)) and 0 < int(m[1]) < 65536})
        extension = _flag(command, '--extension_server_port')
        extension_token = _flag(command, '--extension_server_csrf_token') or token
        for port in ports[:8]:
            endpoints.append(Endpoint(pid, port, 'https', token))
            # Cleartext is allowed only for the app-advertised, process-owned extension port.
            if extension and extension.isdigit() and int(extension) == port:
                endpoints.append(Endpoint(pid, port, 'http', extension_token))
        if len(endpoints) >= 16:
            break
    return endpoints[:16]


def local_json(endpoint, method, body, timeout):
    if (method not in METHODS or type(endpoint.port) is not int
            or not 0 < endpoint.port < 65536 or endpoint.scheme not in ('https', 'http')
            or not isinstance(endpoint.token,str) or not endpoint.token or len(endpoint.token)>4096
            or '\r' in endpoint.token or '\n' in endpoint.token
            or isinstance(timeout,bool) or not 0 < timeout <= 18):
        raise ValueError('Invalid local quota endpoint')
    # The app uses a self-signed certificate. This context is used only for
    # numeric IPv4 loopback, discovered from this user's official app sockets.
    handlers = [urllib.request.ProxyHandler({}), NoRedirect(),
                urllib.request.HTTPSHandler(context=ssl._create_unverified_context())]
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(f'{endpoint.scheme}://127.0.0.1:{endpoint.port}{SERVICE}{method}',
        data=json.dumps(body, allow_nan=False).encode(), headers={
            'Content-Type': 'application/json', 'Connect-Protocol-Version': '1',
            'X-Codeium-Csrf-Token': endpoint.token, 'User-Agent': 'Headroom/0.1'})
    with opener.open(request, timeout=timeout) as response:
        raw = response.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError('Local quota response exceeds limit')
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError('Local quota object required')
        return result


def _text(value, limit=120):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or '@' in value or any(ord(c) < 32 for c in value)):
        return None
    return value.strip()


def _valid_response(payload):
    if not isinstance(payload, dict):
        raise ValueError('Quota object required')
    code = payload.get('code')
    if code is not None and not (type(code) is int and code == 0 or
                                 isinstance(code, str) and code.lower() in ('0', 'ok', 'success')):
        raise ValueError('Quota response unsuccessful')


def _identity(payload):
    _valid_response(payload)
    status = payload.get('userStatus')
    if not isinstance(status, dict):
        raise ValueError('User status required')
    email = status.get('email')
    if not isinstance(email, str) or not email.strip() or len(email) > 320 or any(ord(c) < 32 for c in email):
        raise ValueError('Account continuity unavailable')
    return email.strip().lower(), status


def _window(key, label, fraction, reset, minutes, stamp, disabled=False):
    value = number(fraction)
    known = value is not None and 0 <= value <= 1 and not disabled
    deadline = timestamp(reset)
    # Numeric epoch strings are also used by the local app protocol.
    if deadline is None and isinstance(reset, str):
        epoch = number(reset)
        if epoch is not None:
            from datetime import datetime, timezone
            try: deadline = datetime.fromtimestamp(float(epoch), timezone.utc)
            except (ValueError, OverflowError, OSError): pass
    return {'id': key, 'label': label, 'used_pct': float(100 - value * 100) if known else None,
            'remaining_pct': float(value * 100) if known else None,
            'resets_at': iso(deadline) if deadline else None, 'window_minutes': minutes,
            'status': 'fresh' if known else 'unknown', 'observed_at': stamp, 'source': SOURCE}


def _summary_windows(payload, stamp):
    _valid_response(payload)
    root = payload.get('response') or payload.get('summary') or payload
    groups = root.get('groups') if isinstance(root, dict) else None
    if not isinstance(groups, list):
        return []
    windows = []
    taken = set()
    for group in groups[:16]:
        if not isinstance(group, dict): continue
        title = _text(group.get('displayName') or group.get('name')) or 'Quota'
        if 'gemini' in title.lower(): title = 'Gemini'
        elif any(s in title.lower() for s in ('claude', 'gpt')): title = 'Claude/GPT'
        buckets = group.get('buckets')
        if not isinstance(buckets, list): continue
        for bucket in buckets[:16]:
            if not isinstance(bucket, dict): continue
            key = _text(bucket.get('bucketId') or bucket.get('id'))
            if not key or not re.fullmatch(r'[A-Za-z0-9_-]+', key) or key in taken:
                continue
            taken.add(key)
            label = _text(bucket.get('displayName') or bucket.get('name')) or key
            cadence = _text(bucket.get('window'))
            candidates = [cadence] if cadence else [key, label]
            candidates = {s.lower().replace('_', '-').removesuffix(' limit') for s in candidates}
            aliases = ('session', '5h', '5-hour', 'five hour', 'five-hour', 'weekly')
            candidates |= {alias for s in candidates for alias in aliases if s.endswith('-' + alias)}
            minutes = 300 if candidates.intersection(aliases[:-1]) else 10080 if 'weekly' in candidates else None
            if minutes: label = '5-hour' if minutes == 300 else 'weekly'
            fraction = bucket.get('remainingFraction')
            remaining = bucket.get('remaining')
            if fraction is None and isinstance(remaining, dict):
                fraction = remaining.get('remainingFraction')
                if fraction is None and remaining.get('case') == 'remainingFraction':
                    fraction = remaining.get('value')
            windows.append(_window('antigravity-quota-summary-' + key, title + ' ' + label,
                fraction, bucket.get('resetTime'), minutes, stamp, bucket.get('disabled') is True))
            if len(windows) >= 32: return windows
    return windows


def normalize(status_payload, store, summary=None, observed_at=None):
    stamp = observed_at or iso()
    email, status = _identity(status_payload)
    windows = _summary_windows(summary, stamp) if summary is not None else []
    if not any(w['status'] == 'fresh' for w in windows):
        data = status.get('cascadeModelConfigData')
        models = data.get('clientModelConfigs') if isinstance(data, dict) else None
        windows = []
        for model in (models if isinstance(models, list) else [])[:32]:
            if not isinstance(model, dict) or not isinstance(model.get('quotaInfo'), dict): continue
            alias = model.get('modelOrAlias')
            key = _text(alias.get('model')) if isinstance(alias, dict) else None
            if not key: continue
            quota = model['quotaInfo']
            windows.append(_window('model-' + key, _text(model.get('label')) or key,
                quota.get('remainingFraction'), quota.get('resetTime'), None, stamp))
        if windows and all(w['remaining_pct'] == 100 and w['resets_at'] is None for w in windows):
            for w in windows: w.update(used_pct=None, remaining_pct=None, status='unknown')
    tier = status.get('userTier')
    tier = _text(tier.get('name')) if isinstance(tier, dict) else None
    plan = status.get('planStatus')
    info = plan.get('planInfo') if isinstance(plan, dict) else None
    if tier is None and isinstance(info, dict):
        tier = next((_text(info.get(k)) for k in ('planDisplayName', 'displayName', 'productName',
                     'planName', 'planShortName') if _text(info.get(k))), None)
    return {'id': 'antigravity', 'observed_at': stamp, 'source': SOURCE,
            'status': 'verified' if windows and all(w['status'] == 'fresh' for w in windows) else 'partial',
            'verified_tier': tier, 'auth_method': 'local_app', 'auth_verified': True,
            'account_ref': store.fingerprint('antigravity|' + email), 'windows': windows, 'wallet': None,
            'error': None if windows else {'code': 'no_quota_data', 'message': 'Antigravity reported no quota windows.'}}


def failure(code, message, status='unavailable'):
    return {'id': 'antigravity', 'observed_at': iso(), 'source': SOURCE, 'status': status,
            'verified_tier': None, 'auth_method': 'local_app', 'auth_verified': False,
            'account_ref': None, 'windows': [], 'wallet': None, 'error': {'code': code, 'message': message}}


def collect(store):
    deadline = time.monotonic() + 18
    results = {}
    completed_pids = set()
    try:
        endpoints = discover(min(deadline, time.monotonic()+9))
        for endpoint in endpoints:
            if time.monotonic() >= deadline: break
            if endpoint.pid in completed_pids: continue
            try:
                stamp = iso()
                status = local_json(endpoint, 'GetUserStatus', BODY, min(3, deadline-time.monotonic()))
                email, _ = _identity(status)
                summary = None
                if deadline-time.monotonic() > 2:
                    try:
                        summary = local_json(endpoint, 'RetrieveUserQuotaSummary', {'forceRefresh': True},
                                             min(3, deadline-time.monotonic()))
                        # Bind separate quota/identity replies to one continuously signed-in account.
                        after = local_json(endpoint, 'GetUserStatus', BODY, min(3, deadline-time.monotonic()))
                        if _identity(after)[0] != email:
                            continue
                    except (OSError, ValueError, TypeError):
                        summary = None
                result = normalize(status, store, summary, stamp)
                old = results.get(result['account_ref'])
                if old is None or len(result['windows']) > len(old['windows']):
                    results[result['account_ref']] = result
                if result['windows']: completed_pids.add(endpoint.pid)
            except (OSError, ValueError, TypeError):
                continue
        if len(results) > 1:
            return failure('ambiguous_accounts', 'Multiple signed-in Antigravity accounts were detected; quota was withheld.', 'error')
        if results: return next(iter(results.values()))
        if not endpoints:
            return failure('app_not_running', 'Open the signed-in Antigravity app to connect local quotas.')
        return failure('quota_unavailable', 'Antigravity local quotas could not be verified; retrying automatically.', 'error')
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
        return failure('app_unavailable', 'Antigravity app discovery is unavailable; retrying automatically.')
