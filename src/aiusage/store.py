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
import fcntl
import tempfile
from datetime import timedelta
from .analytics import iso, timestamp, number, velocity, utc_now
from .registry import DATA_DIR, DEFAULT_SETTINGS, PROVIDERS, TIMEZONE, MAX_DATA_AGE_SECONDS

class StorageError(RuntimeError):pass

# Historic authenticated session imports used duration-qualified ids for the
# single global Codex limits.  Current provider windows use the short ids.
# Read paths may join them only for that provider, the same account (enforced
# by the caller's account filter), and the exact recorded duration.  Stored
# rows are never renamed, and model-specific windows use different ids, so
# they cannot be joined by this map.
CODEX_WINDOW_ALIASES={'primary-300m':('primary',300),'secondary-10080m':('secondary',10080)}
CODEX_CURRENT_WINDOWS={current:(legacy,minutes) for legacy,(current,minutes) in CODEX_WINDOW_ALIASES.items()}


def canonical_codex_window(provider,window_id,window_minutes):
    if provider!='codex':return window_id
    alias=CODEX_WINDOW_ALIASES.get(window_id)
    return alias[0] if alias and type(window_minutes) is int and window_minutes==alias[1] else window_id


def validated_settings(raw):
    raw=raw if isinstance(raw,dict) else {};result=dict(DEFAULT_SETTINGS)
    interval=raw.get('refresh_interval_seconds')
    if type(interval) is int and 10<=interval<=60:result['refresh_interval_seconds']=interval
    for key in ('warn_remaining_pct','critical_remaining_pct'):
        value=number(raw.get(key))
        if value is not None and 0<=value<=100:result[key]=float(value)
    result['critical_remaining_pct']=min(result['critical_remaining_pct'],result['warn_remaining_pct'])
    return result


def _nonblank(value,limit):
    return isinstance(value,str) and bool(value.strip()) and len(value)<=limit


def valid_state(raw,provider):
    if not isinstance(raw,dict) or raw.get('id')!=provider or raw.get('status') not in ('verified','partial','stale','auth_required','unavailable','error'):return False
    windows=raw.get('windows')
    if not isinstance(windows,list) or len(windows)>32:return False
    for key,limit in (('verified_tier',80),('source',120),('auth_method',40),('account_ref',256)):
        value=raw.get(key)
        if value is not None and (not isinstance(value,str) or len(value)>limit):return False
    error=raw.get('error')
    if error is not None:
        if not isinstance(error,dict) or not _nonblank(error.get('code'),120) or not _nonblank(error.get('message'),1000):return False
    wallet=raw.get('wallet')
    if wallet is not None:
        if not isinstance(wallet,dict) or not wallet:return False
        balance=number(wallet.get('balance'))
        if balance is None or balance<0:return False
        for key,limit in (('unit',80),('label',200)):
            if wallet.get(key) is not None and not _nonblank(wallet[key],limit):return False
    for window in windows:
        if not isinstance(window,dict) or not _nonblank(window.get('id'),160) or not _nonblank(window.get('label'),200):return False
        if window.get('status') not in ('fresh','unlimited','unknown','stale','expired'):return False
        minutes=window.get('window_minutes')
        if minutes is not None and (type(minutes) is not int or minutes<=0):return False
        for key in ('used_pct','remaining_pct'):
            if window.get(key) is not None and number(window[key]) is None:return False
    return True


class Store:
    def __init__(self, root=DATA_DIR):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.path = self.root / 'plans.sqlite3'
        self.lock = threading.RLock()
        self.archive_lock=threading.RLock()
        salt_path = self.root / 'identity-salt'
        lock=os.open(self.root/'identity-init.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            fcntl.flock(lock,fcntl.LOCK_EX)
            if salt_path.is_symlink():raise StorageError('Identity salt must not be a symbolic link.')
            if not salt_path.exists():
                fd,name=tempfile.mkstemp(prefix='.identity-',dir=self.root)
                with os.fdopen(fd,'wb') as stream:stream.write(secrets.token_bytes(32));stream.flush();os.fsync(stream.fileno())
                os.replace(name,salt_path)
            salt_path.chmod(0o600);self.salt=salt_path.read_bytes()
            if len(self.salt)!=32:raise StorageError('Identity salt is damaged; restore the existing salt and history together. No replacement was generated.')
        finally:os.close(lock)
        with self.connect() as db:
            if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise StorageError('Quota database integrity failed; preserve it and restore a verified backup.')
            version=db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0,1):raise StorageError('Unsupported quota database schema; the database was preserved.')
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
                CREATE INDEX IF NOT EXISTS observations_time ON observations(observed_at);
                CREATE TABLE IF NOT EXISTS imports(path TEXT PRIMARY KEY, inode TEXT NOT NULL, offset INTEGER NOT NULL, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS alerts (
                  id TEXT PRIMARY KEY,provider_id TEXT NOT NULL,window_id TEXT NOT NULL,
                  kind TEXT NOT NULL,message TEXT NOT NULL,created_at TEXT NOT NULL);
            ''')
            db.execute('INSERT OR IGNORE INTO metadata(key,value) VALUES (?,?)', ('settings', json.dumps(DEFAULT_SETTINGS)))
            required={'providers':{'id','state','last_attempt'},'observations':{'provider_id','account_ref','window_id','observed_at','resets_at','window_minutes','used_pct','remaining_pct','status','label','source'},
                'imports':{'path','inode','offset','state'},'alerts':{'id','provider_id','window_id','kind','message','created_at'},'metadata':{'key','value'}}
            for table,columns in required.items():
                present={row[1] for row in db.execute('PRAGMA table_info('+table+')')}
                if not columns<=present:raise StorageError('Quota schema is incomplete; the original database was preserved.')
            db.execute('PRAGMA user_version=1')
            self.repair_records(db)
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

    def repair_records(self,db):
        row=db.execute("SELECT value FROM metadata WHERE key='settings'").fetchone()
        try:raw=json.loads(row[0]) if row else {}
        except ValueError:raw={}
        settings=validated_settings(raw)
        if raw!=settings:
            if row:
                key='quarantined-settings-'+hashlib.sha256(row[0].encode()).hexdigest()[:16]+'-'+secrets.token_hex(4)
                db.execute('UPDATE metadata SET key=? WHERE key=?',(key,'settings'))
            db.execute('INSERT OR REPLACE INTO metadata(key,value) VALUES (?,?)',('settings',json.dumps(settings)))
        for row in db.execute('SELECT id,state FROM providers').fetchall():
            if row['id'] not in PROVIDERS:continue
            try:raw=json.loads(row['state'])
            except ValueError:raw=None
            if not valid_state(raw,row['id']):
                key='quarantined-'+row['id']+'-'+hashlib.sha256(row['state'].encode()).hexdigest()[:16]+'-'+secrets.token_hex(4)
                db.execute('UPDATE providers SET id=? WHERE id=?',(key,row['id']))

    def settings(self,db=None):
        if db is None:
            with self.connect() as connection:return self.settings(connection)
        row=db.execute("SELECT value FROM metadata WHERE key='settings'").fetchone()
        try:raw=json.loads(row[0]) if row else {}
        except ValueError:raw={}
        return validated_settings(raw)

    def save_settings(self, values):
        if not isinstance(values, dict) or set(values) - set(DEFAULT_SETTINGS): raise ValueError('Unknown settings')
        updated = {**self.settings(), **values}
        interval = updated['refresh_interval_seconds']
        if type(interval) is not int or not 10 <= interval <= 60: raise ValueError('Refresh interval must be 10–60 seconds under the freshness policy')
        warn, critical = number(updated['warn_remaining_pct']), number(updated['critical_remaining_pct'])
        if warn is None or critical is None or not 0 <= critical <= warn <= 100: raise ValueError('Thresholds must satisfy 0 ≤ critical ≤ warning ≤ 100')
        updated.update(warn_remaining_pct=float(warn), critical_remaining_pct=float(critical))
        with self.lock, self.connect() as db:
            db.execute('INSERT OR REPLACE INTO metadata(key,value) VALUES (?,?)', ('settings', json.dumps(updated)))
        return updated

    def record_window(self, provider, account, window, db=None):
        if db is None:
            with self.lock, self.connect() as connection:
                return self.record_window(provider, account, window, connection)
        if provider not in PROVIDERS: raise ValueError('Unknown provider')
        observed = timestamp(window.get('observed_at'))
        if not observed: return
        if observed<utc_now()-timedelta(days=93):
            archived=self.root/'archives'/(iso(observed)[:7]+'.sqlite3')
            if archived.exists():
                if archived.is_symlink():raise StorageError('Archive file is invalid.')
                cold=sqlite3.connect(archived.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)
                try:
                    row=cold.execute('SELECT 1 FROM observations WHERE provider_id=? AND account_ref=? AND window_id=? AND observed_at=?',
                        (provider,account or 'unscoped',str(window['id'])[:160],iso(observed))).fetchone()
                    if row:return
                finally:cold.close()
        used, remaining = number(window.get('used_pct')), number(window.get('remaining_pct'))
        if used is not None and used < 0: used = None
        if remaining is not None and not 0 <= remaining <= 100: remaining = None
        db.execute('''INSERT OR IGNORE INTO observations(provider_id,account_ref,window_id,observed_at,resets_at,window_minutes,used_pct,remaining_pct,status,label,source) VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
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
        windows=[]
        for entry in (result.get('windows') or []):
            if not isinstance(entry,dict):continue
            window={k:entry.get(k) for k in window_fields}
            if not isinstance(window.get('id'),str) or not window['id'].strip():continue
            if not isinstance(window.get('label'),str) or not window['label'].strip():window['label']=window['id']
            if not isinstance(window.get('source'),str) or not window['source'].strip():window['source']=result.get('source') if isinstance(result.get('source'),str) and result['source'].strip() else 'unknown'
            windows.append(window)
        result['windows']=windows[:32]
        wallet=result.get('wallet')
        if isinstance(wallet,dict) and number(wallet.get('balance')) is not None and number(wallet.get('balance'))>=0:
            result['wallet']={k:wallet.get(k) for k in ('balance','unit','label')
                if k=='balance' or (isinstance(wallet.get(k),str) and wallet[k].strip())}
        else:result['wallet']=None
        error=result.get('error')
        if isinstance(error,dict) and isinstance(error.get('code'),str) and error['code'].strip() and isinstance(error.get('message'),str) and error['message'].strip():
            result['error']={k:error.get(k) for k in ('code','message')}
        elif error:result['error']={'code':'provider_error','message':'Provider reported an error.'}
        else:result['error']=None
        provider = result['id']
        if provider not in PROVIDERS: raise ValueError('Unknown provider')
        # Collectors return a strictly sanitized DTO. Never store raw account JSON.
        with self.lock, self.connect() as db:
            prior = db.execute('SELECT state FROM providers WHERE id=?', (provider,)).fetchone()
            try:previous=json.loads(prior[0]) if prior else None
            except ValueError:previous=None
            if prior and not valid_state(previous,provider):
                key='quarantined-'+provider+'-'+hashlib.sha256(prior[0].encode()).hexdigest()[:16]+'-'+secrets.token_hex(4)
                db.execute('UPDATE providers SET id=? WHERE id=?',(key,provider));previous=None
            retain=(result.get('status') in ('auth_required','unavailable','error') or
                    result.get('error') and not any(w.get('status') in ('fresh','unlimited') for w in result['windows']))
            if retain and previous:
                # Retain last-known counters for display, explicitly stale, without asserting auth remains valid.
                result = {**result, 'windows': previous.get('windows', []),
                          'account_ref': previous.get('account_ref'),
                          'verified_tier': previous.get('verified_tier'),
                          'tier_verified_at': previous.get('tier_verified_at'),
                          'observed_at': previous.get('observed_at')}
                for window in result['windows']: window['status'] = 'stale'
            for window in result.get('windows', []):
                self.record_window(provider, result.get('account_ref'), window, db)
            db.execute('INSERT OR REPLACE INTO providers(id,state,last_attempt) VALUES (?,?,?)',
                       (provider, json.dumps(result), iso()))
        portfolio=self.portfolio()
        alerts=portfolio['alerts']
        if alerts:
            with self.connect() as db:
                for alert in alerts:
                    db.execute('INSERT OR IGNORE INTO alerts(id,provider_id,window_id,kind,message,created_at) VALUES (?,?,?,?,?,?)',
                        (alert['id'],alert['provider_id'],alert['window_id'],alert['kind'],alert['message'],iso()))

    def history(self, provider, hours=24, account=None):
        if provider not in PROVIDERS: raise ValueError('Unknown provider')
        cutoff = iso(utc_now()-timedelta(hours=hours))
        with self.connect() as db:
            if account is None:
                row = db.execute('SELECT state FROM providers WHERE id=?', (provider,)).fetchone()
                try:raw=json.loads(row[0]) if row else {}
                except ValueError:raw={}
                account=raw.get('account_ref') if isinstance(raw,dict) else None
            rows = [dict(r) for r in db.execute('''SELECT * FROM observations WHERE provider_id=?
                AND account_ref=? AND observed_at>=? ORDER BY observed_at''', (provider, account or 'unscoped', cutoff))]
        series = {}
        for row in rows:
            window_id=canonical_codex_window(provider,row['window_id'],row.get('window_minutes'))
            group = series.setdefault(window_id, {'window_id': window_id, 'label': row['label'], 'points': [], '_origins': {}})
            point={k: (float(number(row[k])) if number(row[k]) is not None else None)
                if k in ('used_pct','remaining_pct') else row[k]
                for k in ('observed_at','remaining_pct','used_pct','resets_at')}
            stamp=point['observed_at'];previous=group['_origins'].get(stamp)
            # A canonical current row and its legacy alias can describe the
            # same sample.  Keep one point, preferring the canonical row.
            if previous is None:group['_origins'][stamp]=row['window_id'];group['points'].append(point)
            elif previous!=window_id and row['window_id']==window_id:group['points'][group['points'].index(next(item for item in group['points'] if item['observed_at']==stamp))]=point;group['_origins'][stamp]=row['window_id']
        for group in series.values():group.pop('_origins',None)
        return {'provider': provider, 'series': list(series.values())}

    def portfolio(self, synchronizing=False):
        with self.connect() as db:
            db.execute('BEGIN')
            return self._portfolio(db,synchronizing)

    def _portfolio(self,db,synchronizing=False):
        now = utc_now()
        settings = self.settings(db)
        stale_seconds = MAX_DATA_AGE_SECONDS
        states={}
        for row in db.execute('SELECT * FROM providers'):
            if row['id'] not in PROVIDERS:continue
            try:raw=json.loads(row['state'])
            except ValueError:continue
            if valid_state(raw,row['id']):states[row['id']]=(raw,row['last_attempt'])
        providers = []
        for provider, spec in PROVIDERS.items():
            raw, attempted = states.get(provider, ({'id':provider,'status':'unavailable','auth_method':'unknown',
                'auth_verified':False,'verified_tier':None,'windows':[],'source':'not_connected',
                'error':{'code':'not_connected','message':'Waiting for authenticated account verification.'}}, None))
            windows = []
            account = raw.get('account_ref')
            for entry in raw.get('windows', []):
                w = dict(entry)
                for key in ('used_pct','remaining_pct'):
                    value=number(w.get(key))
                    w[key]=float(value) if value is not None and value>=0 and (key!='remaining_pct' or value<=100) else None
                if w['status']=='fresh' and w['remaining_pct'] is None:w['status']='unknown'
                stamp, reset = timestamp(w.get('observed_at')), timestamp(w.get('resets_at'))
                if reset and reset <= now: w['status'] = 'expired'
                elif not stamp or stamp>now+timedelta(seconds=5) or (now-stamp).total_seconds() >= stale_seconds: w['status'] = 'stale'
                if stamp:w['fresh_until']=iso(stamp+timedelta(seconds=stale_seconds))
                if w['status'] in ('stale','expired'):
                    w['last_known_remaining_pct']=w.get('remaining_pct')
                    w['remaining_pct']=None
                    w['used_pct']=None
                conditions=['window_id=?'];point_params=[provider, account or 'unscoped', w['id']]
                if provider=='codex':
                    aliases=[]
                    legacy=CODEX_CURRENT_WINDOWS.get(w['id'])
                    if legacy and type(w.get('window_minutes')) is int and w['window_minutes']==legacy[1]:aliases.append(legacy)
                    current=CODEX_WINDOW_ALIASES.get(w['id'])
                    if current and type(w.get('window_minutes')) is int and w['window_minutes']==current[1]:aliases.append((current[0],current[1]))
                    for alias_id,alias_minutes in aliases:
                        conditions.append('(window_id=? AND window_minutes=?)');point_params.extend((alias_id,alias_minutes))
                points = [dict(r) for r in db.execute(f'''SELECT * FROM observations
                        WHERE provider_id=? AND account_ref=? AND ({' OR '.join(conditions)}) AND observed_at>=?
                        ORDER BY observed_at DESC, window_id DESC LIMIT 120''',
                        (*point_params, iso(now-timedelta(hours=6))))]
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
            if wallet and (status not in ('verified','partial') or not observed or observed>now+timedelta(seconds=5) or (now-observed).total_seconds()>=stale_seconds):
                wallet = None
            elif wallet:
                wallet = {**wallet,'observed_at':raw.get('observed_at'),'fresh_until':iso(observed+timedelta(seconds=stale_seconds))}
            providers.append({'id':provider,'name':spec['name'],'status':status,
                'plan':{'declared_tier':None,'verified_tier':raw.get('verified_tier'),
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
        return active

    def maintenance(self,now=None):
        from .quota_archive import archive
        return archive(self,now)
