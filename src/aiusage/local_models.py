"""Private, local, model-independent Codex accounting. No prompts or credentials."""
import datetime as dt
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time

HOME = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
DEFAULT_DB = HOME / 'model-usage' / 'data' / 'usage.sqlite3'
FIELDS = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
          'output_tokens', 'reasoning_output_tokens', 'total_tokens')


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z')


def integer(value):
    return value if type(value) is int and 0 <= value <= 10**15 else None


def clean_text(value, fallback='', limit=240):
    return (value[:limit] or fallback) if isinstance(value, str) and not any(ord(c) < 32 for c in value) else fallback


def normalize_usage(raw):
    """Missing/malformed counters remain NULL. Detail counters are subsets."""
    if not isinstance(raw, dict):
        return dict.fromkeys(FIELDS)
    result = {k: integer(raw.get(k)) for k in FIELDS}
    inp = raw.get('input_tokens_details') or raw.get('prompt_tokens_details')
    out = raw.get('output_tokens_details') or raw.get('completion_tokens_details')
    if isinstance(inp, dict):
        result['cached_input_tokens'] = integer(inp.get('cached_tokens'))
        if inp.get('cache_write_tokens') is not None:
            result['cache_write_input_tokens'] = integer(inp.get('cache_write_tokens'))
    if isinstance(out, dict):
        result['reasoning_output_tokens'] = integer(out.get('reasoning_tokens'))
    for dst, src in [('input_tokens', 'prompt_tokens'), ('output_tokens', 'completion_tokens')]:
        if result[dst] is None:
            result[dst] = integer(raw.get(src))
    i, o, t = (result[k] for k in ('input_tokens', 'output_tokens', 'total_tokens'))
    if i is not None and o is not None:
        if t is None:
            result['total_tokens'] = i + o
        elif t != i + o:
            return dict.fromkeys(FIELDS)
    for child, parent in [('cached_input_tokens', 'input_tokens'),
                          ('reasoning_output_tokens', 'output_tokens')]:
        if result[child] is not None and result[parent] is not None and result[child] > result[parent]:
            result[child] = None
    return result


def provider_for(model):
    if model.startswith('muse-'):
        return 'Meta'
    if model.startswith(('gpt-', 'codex-')):
        return 'OpenAI'
    return 'Other / unknown'


class Store:
    def __init__(self, path=DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        self.lock = threading.RLock()
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS events (
                  key TEXT PRIMARY KEY, timestamp TEXT NOT NULL, model TEXT NOT NULL,
                  requested_model TEXT, provider TEXT NOT NULL, thread_id TEXT,
                  turn_id TEXT, response_id TEXT, source TEXT NOT NULL,
                  status TEXT NOT NULL, http_status INTEGER, attempts INTEGER,
                  duration_ms INTEGER, input_tokens INTEGER, cached_input_tokens INTEGER,
                  cache_write_input_tokens INTEGER, output_tokens INTEGER,
                  reasoning_output_tokens INTEGER, total_tokens INTEGER,
                  receipt INTEGER NOT NULL DEFAULT 0, router INTEGER NOT NULL DEFAULT 0,
                  legacy INTEGER NOT NULL DEFAULT 0, conflict INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS events_time ON events(timestamp);
                CREATE INDEX IF NOT EXISTS events_model ON events(model);
                CREATE INDEX IF NOT EXISTS events_match ON events(model,input_tokens,output_tokens);
                CREATE INDEX IF NOT EXISTS events_thread ON events(thread_id,model,timestamp);
                CREATE TABLE IF NOT EXISTS files (
                  path TEXT PRIMARY KEY, inode TEXT NOT NULL, offset INTEGER NOT NULL,
                  prefix TEXT NOT NULL, state TEXT NOT NULL, errors INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            ''')
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=20)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=20000')
        try:
            with db:
                yield db
        finally:
            db.close()

    def observe(self, row, db=None):
        if db is None:
            with self.lock, self.connect() as conn:
                self.observe(row, conn)
            return
        row = dict(row)
        row.setdefault('timestamp', now())
        row.setdefault('model', 'unknown')
        row.setdefault('provider', provider_for(row['model']))
        row.setdefault('status', 'completed')
        row.setdefault('source', 'receipt')
        for k in ('receipt', 'router', 'legacy', 'conflict'):
            row.setdefault(k, 0)
        previous = db.execute('SELECT * FROM events WHERE key=?', (row['key'],)).fetchone()
        # Some providers omit response IDs and Codex creates its own. Reconcile
        # one-to-one on known counters, model, and completion time, never text.
        # Legacy count-only observations can likewise be upgraded by later receipts.
        if previous is None and row.get('receipt') and all(row.get(k) is not None for k in ('input_tokens', 'output_tokens')):
            stamp = row['timestamp']
            match = db.execute('''SELECT * FROM events WHERE receipt=0 AND model=?
                AND input_tokens=? AND output_tokens=?
                AND ((router=1 AND response_id IS NULL AND duration_ms IS NOT NULL
                      AND abs((julianday(timestamp) + duration_ms/86400000.0 - julianday(?))*86400) < 5)
                     OR (legacy=1 AND abs((julianday(timestamp)-julianday(?))*86400) < 5))
                AND (thread_id IS NULL OR ? IS NULL OR thread_id=?)
                ORDER BY timestamp LIMIT 1''',
                (row['model'], row['input_tokens'], row['output_tokens'], stamp, stamp,
                 row.get('thread_id'), row.get('thread_id'))).fetchone()
            if match:
                previous = match
                db.execute('DELETE FROM events WHERE key=?', (match['key'],))
                row['source'] = 'matched'
        if previous:
            old = dict(previous)
            for k in FIELDS:
                if old[k] is not None and row.get(k) is not None and old[k] != row[k]:
                    row['conflict'] = 1
                    if old['receipt']:
                        row[k] = old[k]
            if row['model'] == 'unknown' and old['model'] != 'unknown':
                row['model'], row['provider'] = old['model'], old['provider']
            elif old['receipt'] and row['receipt'] and old['model'] != 'unknown' and row['model'] != old['model']:
                row['conflict'] = 1
                row['model'], row['provider'] = old['model'], old['provider']
            # Router knows the actual returned model and outcome; receipts fill gaps.
            if old['router'] and not row['router']:
                for k in ('model', 'requested_model', 'status', 'http_status', 'attempts', 'duration_ms', 'provider'):
                    row[k] = old[k]
            for k in ('receipt', 'router', 'legacy', 'conflict'):
                row[k] = max(old[k], row[k])
            for k, value in old.items():
                if k not in row or row[k] is None:
                    row[k] = value
            matched = row['source'] == 'matched' or 'matched' in old['source']
            row['source'] = '+'.join(k for k in ('receipt', 'router', 'legacy') if row.get(k))
            if matched:
                row['source'] += '+matched'
            # Preserve first observation time so merges do not move reporting windows.
            row['timestamp'] = min(old['timestamp'], row['timestamp'])
        columns = list(row)
        db.execute('INSERT INTO events (' + ','.join(columns) + ') VALUES (' +
                   ','.join('?' for _ in columns) + ') ON CONFLICT(key) DO UPDATE SET ' +
                   ','.join(k + '=excluded.' + k for k in columns if k != 'key'),
                   [row[k] for k in columns])

    def start_request(self, request_id, model, thread_id=None, turn_id=None):
        self.observe(dict(key='q:' + request_id, model=clean_text(model, 'unknown'),
                          requested_model=clean_text(model, 'unknown'),
                          thread_id=clean_text(thread_id) or None,
                          turn_id=clean_text(turn_id) or None, source='router', router=1,
                          status='in_progress'))

    def finish_request(self, request_id, row):
        with self.lock, self.connect() as db:
            key = 'q:' + request_id
            started = db.execute('SELECT * FROM events WHERE key=?', (key,)).fetchone()
            data = dict(started) if started else {'timestamp': now(), 'model': 'unknown'}
            data.update(row)
            data['provider'] = provider_for(data['model'])
            data.update(source='router', router=1)
            response = clean_text(data.get('response_id'))
            data['key'] = 'r:' + response if response else key
            if response:
                db.execute('DELETE FROM events WHERE key=?', (key,))
            elif all(data.get(k) is not None for k in ('input_tokens', 'output_tokens')):
                match = db.execute('''SELECT * FROM events WHERE receipt=1 AND router=0
                    AND model=? AND input_tokens=? AND output_tokens=?
                    AND abs((julianday(timestamp)-julianday(?))*86400) < 5
                    AND (thread_id IS NULL OR ? IS NULL OR thread_id=?)
                    ORDER BY timestamp LIMIT 1''',
                    (data['model'], data['input_tokens'], data['output_tokens'], now(),
                     data.get('thread_id'), data.get('thread_id'))).fetchone()
                if match:
                    db.execute('DELETE FROM events WHERE key=?', (key,))
                    data['key'] = match['key']
                    data['response_id'] = match['response_id']
                    data['source'] = 'matched'
            self.observe(data, db)

    def set_meta(self, key, value):
        with self.lock, self.connect() as db:
            db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', (key, json.dumps(value)))

    def reconcile_unparsed(self):
        """Upgrade a missed wire receipt only on a unique same-thread end-time match."""
        with self.lock, self.connect() as db:
            orphans = db.execute('''SELECT * FROM events WHERE router=1 AND receipt=0
                AND response_id IS NULL AND total_tokens IS NULL AND thread_id IS NOT NULL
                AND http_status=200 AND duration_ms IS NOT NULL AND status!='in_progress'
                ORDER BY timestamp LIMIT 100''').fetchall()
            for orphan in orphans:
                candidates = db.execute('''SELECT * FROM events WHERE receipt=1 AND router=0
                    AND thread_id=? AND model=? AND total_tokens IS NOT NULL
                    AND abs((julianday(timestamp) - julianday(?) - ?/86400000.0)*86400) < 0.5
                    LIMIT 2''', (orphan['thread_id'], orphan['model'], orphan['timestamp'], orphan['duration_ms'])).fetchall()
                if len(candidates) != 1:
                    continue
                receipt = candidates[0]
                data = dict(orphan)
                db.execute('DELETE FROM events WHERE key=?', (orphan['key'],))
                data.update(key=receipt['key'], response_id=receipt['response_id'], source='matched')
                self.observe(data, db)

    def summary(self, catalog, days=7):
        current = dt.datetime.now(dt.timezone.utc)
        if days == 1:
            bogota = dt.timezone(dt.timedelta(hours=-5))
            cutoff = current.astimezone(bogota).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(dt.timezone.utc)
        else:
            cutoff = current - dt.timedelta(days=days)
        where = 'WHERE julianday(timestamp)>=julianday(?) AND julianday(timestamp)<=julianday(?)' if days else ''
        args = (cutoff.isoformat().replace('+00:00', 'Z'),current.isoformat().replace('+00:00', 'Z')) if days else ()
        columns = ['model', 'max(provider) AS provider', 'count(*) AS requests',
                   'max(timestamp) AS last_seen', 'group_concat(DISTINCT source) AS sources',
                   "sum(status='in_progress') AS active",
                   "sum(status IN ('failed','upstream_error','authentication_error','interrupted','incomplete')) AS errors",
                   'sum(total_tokens IS NULL) AS unknown_usage', 'sum(conflict) AS conflicts',
                   'sum(receipt) AS exact_receipts', 'sum(legacy) AS legacy_estimates',
                   'sum(router) AS router_requests', "sum(source LIKE '%matched%') AS inferred_matches",
                   "sum(router=1 AND receipt=0 AND response_id IS NULL AND total_tokens IS NULL AND http_status=200 AND status!='in_progress') AS unresolved_observations",
                   'sum(' + ' OR '.join(k + ' IS NULL' for k in FIELDS) + ') AS partial_usage']
        for k in FIELDS:
            columns.extend(['coalesce(sum(' + k + '),0) AS ' + k, 'count(' + k + ') AS known_' + k])
        # SQLite WAL and the explicit read transaction provide a consistent
        # snapshot without waiting on the importer's application-level lock.
        with self.connect() as db:
            db.execute('BEGIN')  # One read snapshot for model, daily, and coverage totals.
            records = [dict(r) for r in db.execute('SELECT ' + ','.join(columns) + ' FROM events ' + where + ' GROUP BY model', args)]
            day_rows = [dict(r) for r in db.execute("SELECT substr(datetime(timestamp,'-5 hours'),1,10) AS day, coalesce(sum(total_tokens),0) AS total_tokens, count(*) AS requests FROM events " + where + ' GROUP BY day ORDER BY day', args)]
            meta = {r['key']: json.loads(r['value']) for r in db.execute('SELECT * FROM metadata')}
            file_info = db.execute('SELECT count(*),coalesce(sum(offset),0),coalesce(sum(errors),0) FROM files').fetchone()
        base = dict(requests=0, unknown_usage=0, errors=0, **{k: 0 for k in FIELDS})
        models = {}
        for item in catalog:
            model = clean_text(item.get('slug') or item.get('id'), 'unknown')
            models[model] = dict(base, model=model,
                                 display_name=clean_text(item.get('display_name'), model),
                                 provider=provider_for(model), last_seen=None, source='unused',
                                 partial_usage=0, conflicts=0, known_counts=dict.fromkeys(FIELDS, 0))
        totals = dict(base, partial_usage=0, conflicts=0, known_counts=dict.fromkeys(FIELDS, 0))
        coverage = dict(exact_receipts=0, legacy_estimates=0, router_requests=0)
        inferred_matches = 0
        unresolved = 0
        active = 0
        for row in records:
            model = row['model']
            target = models.setdefault(model, dict(base, model=model, display_name=model,
                                       provider=row['provider'], last_seen=None, source='',
                                       partial_usage=0, conflicts=0, known_counts=dict.fromkeys(FIELDS, 0)))
            for value in (target, totals):
                for k in ('requests', 'unknown_usage', 'partial_usage', 'errors', 'conflicts'):
                    value[k] += row[k]
                for k in FIELDS:
                    value[k] += row[k] or 0
                    value['known_counts'][k] += row['known_' + k]
            target['last_seen'] = row['last_seen']
            sources = set(filter(None, target['source'].split('+'))) - {'unused'}
            for source in row['sources'].split(','):
                sources.update(source.split('+'))
            target['source'] = '+'.join(sorted(sources))
            active += row['active']
            for k in coverage:
                coverage[k] += row[k]
            inferred_matches += row['inferred_matches']
            unresolved += row['unresolved_observations']
        heartbeat = meta.get('router_heartbeat', {})
        instrumented = bool(heartbeat.get('time', 0) > time.time() - 90)
        warnings = ['Tokens reflect local receipts, not subscription credits or billed dollars.',
                    'Cached input and reasoning output are subsets of input and output; do not add them again.',
                    'Daily dates and Today use America/Bogota (UTC-5). Models with no recorded usage remain visible.']
        if coverage['exact_receipts']:
            warnings.append('Historical token receipts report exact counters; their model names come from saved turn context. Calls never recorded locally cannot be reconstructed.')
        if coverage['legacy_estimates']:
            warnings.append('Older history uses deduplicated token-count increments; model attribution is inferred from turn context.')
        if totals['partial_usage']:
            warnings.append('Some token details are unreported; displayed sums include only known values.')
        if totals['errors']:
            warnings.append('Errors include interrupted or incomplete observations; they do not all imply provider rejection.')
        if unresolved:
            warnings.append(str(unresolved) + ' unidentified router observations may overlap with saved receipts. Request counts include these unresolved observations; they add no unknown tokens to the known sums.')
        if totals['conflicts']:
            warnings.append('Conflicting observations detected; inspect the local database before treating totals as billing evidence.')
        if not instrumented:
            warnings.append('Router telemetry is awaiting activation; live saved Codex receipts are still collected.')
        if file_info[2]:
            warnings.append('Malformed historical events were skipped; import error count is shown.')
        if meta.get('history_gaps', 0):
            warnings.append('Some historical counters start mid-session or jump; only identifiable request usage is included.')
        if inferred_matches:
            warnings.append('Provider responses without IDs were matched one-to-one by model, counters, and completion time; these joins are inferred.')
        if meta.get('collector_error'):
            warnings.append('History collector encountered a storage error; retained totals may be stale.')
        if meta.get('scan_failures'):
            warnings.append('Some history files could not be read; the collector will retry them.')
        return dict(updated_at=now(), started_at=meta.get('started_at', now()), collecting=not meta.get('collector_error', False),
                    import_={'files': file_info[0], 'bytes': file_info[1], 'errors': file_info[2],
                             'last_scan': meta.get('last_scan'), 'scanning': meta.get('scanning', False)},
                    totals=totals, models=sorted(models.values(), key=lambda r: (-r['total_tokens'], r['model'])),
                    daily=day_rows, coverage=coverage,
                    router={'instrumented': instrumented, 'active_requests': active}, warnings=warnings)


class Importer:
    """Incremental byte offsets, atomic checkpoints, and global response-ID deduplication."""
    MAX_LINE = 32 * 1024 * 1024

    def __init__(self, store, home=HOME):
        self.store, self.home = store, Path(home)

    def paths(self):
        files = list((self.home / 'sessions').rglob('*.jsonl'))
        files.extend((self.home / 'archived_sessions').rglob('*.jsonl'))
        return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)

    def process_event(self, event, state, db):
        if not isinstance(event, dict):
            return
        typ, payload = event.get('type'), event.get('payload')
        if not isinstance(payload, dict):
            return
        stamp = clean_text(event.get('timestamp'), now())
        if typ == 'session_meta':
            state['session_id'] = clean_text(payload.get('id'), 'unknown')
            state['provider'] = clean_text(payload.get('model_provider'))
            provenance = payload.get('provenance')
            if isinstance(provenance, dict) and provenance.get('type') == 'model':
                state['initial_model'] = clean_text(provenance.get('model'), 'unknown')
        elif typ == 'turn_context':
            state['model'] = clean_text(payload.get('model'), 'unknown')
            state['turn_id'] = clean_text(payload.get('turn_id'))
            turns = state.setdefault('turn_models', {})
            turns[state['turn_id']] = state['model']
        elif typ == 'token_usage_record':
            rid = clean_text(payload.get('response_id'))
            # A stable ordinal plus session protects missing-ID receipts without merging distinct requests.
            identity = 'r:' + rid if rid else 'h:' + hashlib.sha256(json.dumps(
                [state.get('session_id'), event.get('ordinal'), stamp, payload.get('usage')],
                sort_keys=True).encode()).hexdigest()
            turn = clean_text(payload.get('turn_id'))
            model = clean_text(payload.get('model'), state.get('turn_models', {}).get(
                turn, state.get('model', state.get('initial_model', 'unknown'))))
            self.store.observe(dict(key=identity, timestamp=stamp, model=model,
                               thread_id=clean_text(payload.get('thread_id')) or state.get('session_id'),
                               turn_id=clean_text(payload.get('turn_id')) or state.get('turn_id'),
                               response_id=rid or None, source='receipt', receipt=1,
                               **normalize_usage(payload.get('usage'))), db)
            cumulative = payload.get('thread_token_usage')
            if isinstance(cumulative, dict):
                state['covered_total'] = cumulative.get('total_tokens')
            state['receipt_seen'] = True
        elif typ == 'event_msg' and payload.get('type') == 'token_count':
            info = payload.get('info')
            if not isinstance(info, dict):
                return
            total = normalize_usage(info.get('total_token_usage'))
            last = normalize_usage(info.get('last_token_usage'))
            n = total['total_tokens']
            if n is None:
                return
            old = state.get('last_total')
            state['last_total'] = n
            if n == old or n == state.get('covered_total'):
                return
            # Do not add an entire cumulative thread balance or a copied fork baseline.
            if last['total_tokens'] is None or last['total_tokens'] == 0:
                return
            if old is None and n != last['total_tokens'] or old is not None and n > old and n - old != last['total_tokens']:
                state['gaps'] = state.get('gaps', 0) + 1
            identity = 'l:' + hashlib.sha256(json.dumps(
                [stamp, state.get('model', 'unknown'), info.get('total_token_usage')],
                sort_keys=True).encode()).hexdigest()
            self.store.observe(dict(key=identity, timestamp=stamp,
                                   model=state.get('model', 'unknown'),
                                   thread_id=state.get('session_id'), turn_id=state.get('turn_id'),
                                   source='legacy', legacy=1, **last), db)

    def scan_file(self, path):
        stat = path.stat()
        inode = f'{stat.st_dev}:{stat.st_ino}'
        with path.open('rb') as handle:
            prefix = hashlib.sha256(handle.read(min(stat.st_size, 256))).hexdigest()
            with self.store.lock, self.store.connect() as db:
                old = db.execute('SELECT * FROM files WHERE path=?', (str(path),)).fetchone()
                offset, state, errors = 0, {}, 0
                if old and old['inode'] == inode and stat.st_size >= old['offset'] and old['prefix'] == prefix:
                    offset, state, errors = old['offset'], json.loads(old['state']), old['errors']
                if offset == stat.st_size:
                    return
                handle.seek(offset)
                # Bound each transaction so HTTP readers and router writers can progress.
                read_bytes = 0
                while read_bytes < 16 * 1024 * 1024:
                    pos = handle.tell()
                    line = handle.readline(self.MAX_LINE + 1)
                    if not line:
                        break
                    if len(line) > self.MAX_LINE:
                        while line and not line.endswith(b'\n'):
                            line = handle.readline(self.MAX_LINE + 1)
                        errors += 1
                        offset = handle.tell()
                        continue
                    if not line.endswith(b'\n'):
                        handle.seek(pos)
                        break  # A writer may still be appending this record.
                    offset = handle.tell()
                    read_bytes += len(line)
                    # Most of the 3GB history is conversation/tool content; never parse/store it.
                    if not any(t in line[:600] for t in (b'"session_meta"', b'"turn_context"',
                                                         b'"token_usage_record"', b'"event_msg"')):
                        continue
                    try:
                        self.process_event(json.loads(line), state, db)
                    except (ValueError, TypeError, KeyError):
                        errors += 1
                db.execute('INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?)',
                           (str(path), inode, offset, prefix, json.dumps(state), errors))

    def scan(self):
        self.store.set_meta('scanning', True)
        try:
            paths = self.paths()
            failures = 0
            for path in paths:
                try:
                    self.scan_file(path)
                except (OSError, sqlite3.Error):
                    # Preserve existing checkpoints and retry; never log file contents.
                    failures += 1
            with self.store.lock, self.store.connect() as db:
                gaps = sum(json.loads(r[0]).get('gaps', 0) for r in db.execute('SELECT state FROM files'))
            self.store.set_meta('history_gaps', gaps)
            self.store.set_meta('scan_failures', failures)
            self.store.reconcile_unparsed()
            self.store.set_meta('last_scan', now())
        finally:
            self.store.set_meta('scanning', False)
