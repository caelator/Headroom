"""Private SQLite snapshots, sanitized provider state and settings."""
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import re
from datetime import timedelta
from .analytics import iso, timestamp, number, velocity, utc_now
from .registry import DATA_DIR, DEFAULT_SETTINGS, PROVIDERS, TIMEZONE, MAX_DATA_AGE_SECONDS


class Store:
    def __init__(self, root=DATA_DIR):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.path = self.root / 'plans.sqlite3'
        self.lock = threading.RLock()
        salt_path = self.root / 'identity-salt'
        if not salt_path.exists():
            fd = os.open(salt_path, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as f: f.write(secrets.token_bytes(32))
        self.salt = salt_path.read_bytes()
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS providers (id TEXT PRIMARY KEY,state TEXT NOT NULL,last_attempt TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS observations (
                  provider_id TEXT NOT NULL, account_ref TEXT NOT NULL, window_id TEXT NOT NULL,
                  observed_at TEXT NOT NULL, resets_at TEXT, window_minutes INTEGER,
                  used_pct TEXT, remaining_pct TEXT, status TEXT NOT NULL, label TEXT NOT NULL,
                  source TEXT NOT NULL,
                  PRIMARY KEY(provider_id,account_ref,window_id,observed_at));
                CREATE INDEX IF NOT EXISTS observations_history ON observations(provider_id,account_ref,window_id,observed_at);
                CREATE TABLE IF NOT EXISTS imports(path TEXT PRIMARY KEY, inode TEXT NOT NULL, offset INTEGER NOT NULL, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS alerts (
                  id TEXT PRIMARY KEY,provider_id TEXT NOT NULL,window_id TEXT NOT NULL,
                  kind TEXT NOT NULL,message TEXT NOT NULL,created_at TEXT NOT NULL);
            ''')
            db.execute('INSERT OR IGNORE INTO metadata VALUES (?,?)', ('settings', json.dumps(DEFAULT_SETTINGS)))
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=10000')
        try:
            with db: yield db
        finally: db.close()

    def fingerprint(self, value):
        return hmac.new(self.salt, str(value).encode(), hashlib.sha256).hexdigest()[:32]

    def settings(self):
        with self.connect() as db:
            settings=json.loads(db.execute("SELECT value FROM metadata WHERE key='settings'").fetchone()[0])
        # Tighten previously configured intervals to the user's current freshness policy.
        if not 10<=settings['refresh_interval_seconds']<=60:settings['refresh_interval_seconds']=30
        return settings

    def save_settings(self, values):
        if not isinstance(values, dict) or set(values) - set(DEFAULT_SETTINGS): raise ValueError('Unknown settings')
        updated = {**self.settings(), **values}
        interval = updated['refresh_interval_seconds']
        if type(interval) is not int or not 10 <= interval <= 60: raise ValueError('Refresh interval must be 10–60 seconds under the freshness policy')
        warn, critical = number(updated['warn_remaining_pct']), number(updated['critical_remaining_pct'])
        if warn is None or critical is None or not 0 <= critical <= warn <= 100: raise ValueError('Thresholds must satisfy 0 ≤ critical ≤ warning ≤ 100')
        updated.update(warn_remaining_pct=float(warn), critical_remaining_pct=float(critical))
        with self.lock, self.connect() as db:
            db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', ('settings', json.dumps(updated)))
        return updated

    def record_window(self, provider, account, window, db=None):
        if db is None:
            with self.lock, self.connect() as connection:
                return self.record_window(provider, account, window, connection)
        if provider not in PROVIDERS: raise ValueError('Unknown provider')
        observed = timestamp(window.get('observed_at'))
        if not observed: return
        used, remaining = number(window.get('used_pct')), number(window.get('remaining_pct'))
        if used is not None and used < 0: used = None
        if remaining is not None and not 0 <= remaining <= 100: remaining = None
        db.execute('''INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
            (provider, account or 'unscoped', str(window['id'])[:160], iso(observed),
             window.get('resets_at'), window.get('window_minutes'),
             str(used) if used is not None else None,
             str(remaining) if remaining is not None else None,
             window.get('status', 'unknown'), str(window.get('label',window['id']))[:200],
             str(window.get('source','unknown'))[:120]))

    def save_provider(self, result):
        # Storage is an allowlist boundary even if a future adapter accidentally
        # forwards extra account fields. Raw auth/profile payloads never persist.
        allowed=('id','observed_at','source','status','verified_tier','auth_method','auth_verified',
                 'account_ref','windows','wallet','error','tier_verified_at')
        result={k:result.get(k) for k in allowed}
        window_fields=('id','label','used_pct','remaining_pct','resets_at','window_minutes','status','observed_at','source')
        result['windows']=[{k:w.get(k) for k in window_fields} for w in (result.get('windows') or []) if isinstance(w,dict)][:32]
        if result.get('wallet'):
            result['wallet']={k:result['wallet'].get(k) for k in ('balance','unit','label')}
        if result.get('error'):
            result['error']={k:result['error'].get(k) for k in ('code','message')}
        provider = result['id']
        if provider not in PROVIDERS: raise ValueError('Unknown provider')
        # Collectors return a strictly sanitized DTO. Never store raw account JSON.
        with self.lock, self.connect() as db:
            prior = db.execute('SELECT state FROM providers WHERE id=?', (provider,)).fetchone()
            if result.get('status') in ('auth_required','unavailable','error') and prior:
                previous = json.loads(prior[0])
                # Retain last-known counters for display, explicitly stale, without asserting auth remains valid.
                result = {**result, 'windows': previous.get('windows', []),
                          'account_ref': previous.get('account_ref'),
                          'verified_tier': previous.get('verified_tier'),
                          'tier_verified_at': previous.get('tier_verified_at'),
                          'observed_at': previous.get('observed_at')}
                for window in result['windows']: window['status'] = 'stale'
            for window in result.get('windows', []):
                self.record_window(provider, result.get('account_ref'), window, db)
            db.execute('INSERT OR REPLACE INTO providers VALUES (?,?,?)',
                       (provider, json.dumps(result), iso()))

    def history(self, provider, hours=24, account=None):
        if provider not in PROVIDERS: raise ValueError('Unknown provider')
        cutoff = iso(utc_now()-timedelta(hours=hours))
        with self.connect() as db:
            if account is None:
                row = db.execute('SELECT state FROM providers WHERE id=?', (provider,)).fetchone()
                account = json.loads(row[0]).get('account_ref') if row else None
            rows = [dict(r) for r in db.execute('''SELECT * FROM observations WHERE provider_id=?
                AND account_ref=? AND observed_at>=? ORDER BY observed_at''', (provider, account or 'unscoped', cutoff))]
        series = {}
        for row in rows:
            group = series.setdefault(row['window_id'], {'window_id': row['window_id'], 'label': row['label'], 'points': []})
            group['points'].append({k: (float(number(row[k])) if number(row[k]) is not None else None)
                if k in ('used_pct','remaining_pct') else row[k]
                for k in ('observed_at','remaining_pct','used_pct','resets_at')})
        return {'provider': provider, 'series': list(series.values())}

    def portfolio(self, synchronizing=False):
        now = utc_now()
        settings = self.settings()
        stale_seconds = MAX_DATA_AGE_SECONDS
        with self.connect() as db:
            states = {r['id']: (json.loads(r['state']), r['last_attempt']) for r in db.execute('SELECT * FROM providers')}
        providers = []
        for provider, spec in PROVIDERS.items():
            raw, attempted = states.get(provider, ({'id':provider,'status':'unavailable','auth_method':'unknown',
                'auth_verified':False,'verified_tier':None,'windows':[],'source':'not_connected',
                'error':{'code':'not_connected','message':'Waiting for authenticated account verification.'}}, None))
            windows = []
            account = raw.get('account_ref')
            for entry in raw.get('windows', []):
                w = dict(entry)
                stamp, reset = timestamp(w.get('observed_at')), timestamp(w.get('resets_at'))
                if reset and reset <= now: w['status'] = 'expired'
                elif not stamp or stamp>now+timedelta(seconds=5) or (now-stamp).total_seconds() >= stale_seconds: w['status'] = 'stale'
                if stamp:w['fresh_until']=iso(stamp+timedelta(seconds=stale_seconds))
                if w['status'] in ('stale','expired'):
                    w['last_known_remaining_pct']=w.get('remaining_pct')
                    w['remaining_pct']=None
                    w['used_pct']=None
                with self.connect() as db:
                    points = [dict(r) for r in db.execute('''SELECT * FROM observations
                        WHERE provider_id=? AND account_ref=? AND window_id=? AND observed_at>=?
                        ORDER BY observed_at DESC LIMIT 120''',
                        (provider, account or 'unscoped', w['id'], iso(now-timedelta(hours=6))))]
                for p in points: p['account_ref'] = account
                calculation = {**w,'account_ref':account}
                w['velocity'] = velocity(points, calculation, now,
                    max_gap_seconds=MAX_DATA_AGE_SECONDS)
                windows.append(w)
            measured = [w for w in windows if w.get('remaining_pct') is not None and w['status']=='fresh']
            risk=[w for w in measured if w['velocity'].get('exhausts_before_reset')]
            exhausted = [w for w in measured if number(w['remaining_pct']) == 0]
            limiting = exhausted[0] if exhausted else min(risk,key=lambda w:w['velocity']['eta_exhaustion_at']) if risk else min(measured,key=lambda w:w['remaining_pct']) if measured else None
            status = raw.get('status','unavailable')
            if windows and not any(w['status'] in ('fresh','unlimited') for w in windows) and status in ('verified','partial'): status='stale'
            verified_at = (raw.get('tier_verified_at') or raw.get('observed_at')) if raw.get('verified_tier') else None
            wallet = raw.get('wallet')
            observed = timestamp(raw.get('observed_at'))
            if wallet and (status not in ('verified','partial') or not observed or (now-observed).total_seconds()>=stale_seconds):
                wallet = None
            elif wallet:
                wallet = {**wallet,'observed_at':raw.get('observed_at'),'fresh_until':iso(observed+timedelta(seconds=stale_seconds))}
            providers.append({'id':provider,'name':spec['name'],'status':status,
                'plan':{'declared_tier':'Highest plan (user reported)' if provider!='claude' else None,'verified_tier':raw.get('verified_tier'),
                        'verification':'provider' if raw.get('verified_tier') else 'unverified','verified_at':verified_at},
                'auth':{'method':raw.get('auth_method','unknown'),'verified':bool(raw.get('auth_verified'))},
                'remaining_pct':limiting['remaining_pct'] if limiting else None,
                'limiting_window_id':limiting['id'] if limiting else None,
                'windows':windows,'wallet':wallet,'last_attempt_at':attempted,
                'error':raw.get('error'),'connection_url':spec['connection_url'],'source':raw.get('source','unknown'),
                '_account_ref':account})
        alerts = self.alerts(providers, settings)
        for provider in providers:provider.pop('_account_ref',None)
        return {'updated_at':iso(now),'timezone':TIMEZONE,'synchronizing':synchronizing,
                'refresh_interval_seconds':settings['refresh_interval_seconds'],'settings':settings,
                'freshness_policy':{'max_age_seconds':MAX_DATA_AGE_SECONDS,'hide_stale_current':True},
                'providers':providers,'alerts':alerts,
                'warnings':['Percentages are separate plan allowances; they are not summed or averaged across services.',
                            'Forecasts use observed quota changes within a single account and reset window.',
                            'Local token counts do not imply subscription percentages or billed costs.']}

    def alerts(self, providers, settings):
        active = []
        for provider in providers:
            for window in provider['windows']:
                if window.get('status')!='fresh': continue
                remain = number(window.get('remaining_pct'))
                kind = None
                if remain == 0: kind='quota_exhausted'
                elif remain is not None and remain <= number(settings['critical_remaining_pct']): kind='critical_remaining'
                elif remain is not None and remain <= number(settings['warn_remaining_pct']): kind='low_remaining'
                if remain != 0 and window['velocity'].get('exhausts_before_reset'): kind='exhausts_before_reset'
                if not kind: continue
                message = provider['name'] + ' · ' + window['label'] + ': ' + (
                    'allowance exhausted' if kind=='quota_exhausted' else 'projected to run out before reset' if kind=='exhausts_before_reset' else str(float(remain))+'% remaining')
                alert_id = self.fingerprint('|'.join([provider['id'],str(provider.get('_account_ref')),window['id'],str(window.get('resets_at')),kind]))
                active.append({'id':alert_id,'provider_id':provider['id'],'window_id':window['id'],'kind':kind,'message':message})
                with self.connect() as db:
                    db.execute('INSERT OR IGNORE INTO alerts VALUES (?,?,?,?,?,?)',
                               (alert_id,provider['id'],window['id'],kind,message,iso()))
        return active
