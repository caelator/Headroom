"""Bound hot quota history without deleting historical data or identity evidence."""
from datetime import timedelta
import heapq
from pathlib import Path
import re
import sqlite3
import os
from .analytics import iso,utc_now

HOT_DAYS=93
TABLES={
    'observations':('provider_id','account_ref','window_id','observed_at','resets_at','window_minutes','used_pct','remaining_pct','status','label','source'),
    'alerts':('id','provider_id','window_id','kind','message','created_at')}
KEYS={'observations':('provider_id','account_ref','window_id','observed_at'),'alerts':('id','created_at')}
SCHEMA='''CREATE TABLE IF NOT EXISTS observations (
provider_id TEXT NOT NULL,account_ref TEXT NOT NULL,window_id TEXT NOT NULL,observed_at TEXT NOT NULL,
resets_at TEXT,window_minutes INTEGER,used_pct TEXT,remaining_pct TEXT,status TEXT NOT NULL,label TEXT NOT NULL,source TEXT NOT NULL,
PRIMARY KEY(provider_id,account_ref,window_id,observed_at));
CREATE TABLE IF NOT EXISTS alerts (id TEXT,provider_id TEXT,window_id TEXT,kind TEXT,message TEXT,created_at TEXT,PRIMARY KEY(id,created_at));
CREATE INDEX IF NOT EXISTS observations_time ON observations(observed_at);'''


def path_for(root,stamp):
    month=stamp[:7]
    if not re.fullmatch(r'\d{4}-\d{2}',month):raise ValueError('Invalid archive month')
    directory=Path(root)/'archives'
    if directory.is_symlink():raise ValueError('Archive directory must not be a symlink')
    directory.mkdir(parents=True,exist_ok=True,mode=0o700);directory.chmod(0o700)
    return directory/(month+'.sqlite3')


def _ensure_schema(db):
    db.executescript(SCHEMA)
    db.execute('BEGIN IMMEDIATE')
    # Archives written by the first cold-storage shape keyed alerts only by
    # id.  That cannot preserve a later recurrence of the same provider alert
    # (which keeps its fingerprint id but gets a new created_at).  Add any
    # missing columns, then rebuild only that table, copying every row.
    for table,columns in TABLES.items():
        present={row[1] for row in db.execute('PRAGMA table_info('+table+')')}
        for column in columns:
            if column not in present:db.execute(f'ALTER TABLE {table} ADD COLUMN {column} '+('INTEGER' if column=='window_minutes' else 'TEXT'))
    pk=[row[1] for row in sorted((row for row in db.execute('PRAGMA table_info(alerts)') if row[5]),key=lambda row:row[5])]
    if pk!=['id','created_at']:
        columns=','.join(TABLES['alerts'])
        db.execute('CREATE TABLE alerts_migrated (id TEXT,provider_id TEXT,window_id TEXT,kind TEXT,message TEXT,created_at TEXT,PRIMARY KEY(id,created_at))')
        db.execute(f'INSERT OR IGNORE INTO alerts_migrated ({columns}) SELECT {columns} FROM alerts')
        if db.execute('SELECT count(*) FROM alerts_migrated').fetchone()[0]!=db.execute('SELECT count(*) FROM alerts').fetchone()[0]:
            raise ValueError('Archive alert migration conflicts; original records preserved')
        db.execute('DROP TABLE alerts');db.execute('ALTER TABLE alerts_migrated RENAME TO alerts')
    db.execute('CREATE INDEX IF NOT EXISTS observations_time ON observations(observed_at)')


def connect(path):
    if path.is_symlink():raise ValueError('Archive file must not be a symlink')
    descriptor=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600);os.fchmod(descriptor,0o600);os.close(descriptor)
    db=sqlite3.connect(path,timeout=1);db.row_factory=sqlite3.Row
    try:
        db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA synchronous=FULL')
        with db:_ensure_schema(db)
    except Exception:db.close();raise
    path.chmod(0o600);return db


def archive(store,now=None,limit=10000):
    cutoff=iso((now or utc_now())-timedelta(days=HOT_DAYS));moved=0
    with store.archive_lock,store.lock:
        for table,columns in TABLES.items():
            stamp='observed_at' if table=='observations' else 'created_at'
            with store.connect() as active:
                rows=[dict(row) for row in active.execute(f'SELECT * FROM {table} WHERE {stamp}<? ORDER BY {stamp} LIMIT ?', (cutoff,limit))]
            groups={}
            for row in rows:groups.setdefault(path_for(store.root,row[stamp]),[]).append(row)
            for path,records in groups.items():
                db=connect(path)
                try:
                    with db:
                        for row in records:
                            db.execute(f'INSERT OR IGNORE INTO {table} ({",".join(columns)}) VALUES ({",".join("?" for _ in columns)})',tuple(row[k] for k in columns))
                            existing=db.execute(f'SELECT * FROM {table} WHERE '+ ' AND '.join(k+'=?' for k in KEYS[table]),tuple(row[k] for k in KEYS[table])).fetchone()
                            if any(existing[k]!=row[k] for k in columns):raise ValueError('Archive conflict; active records preserved')
                    # The archive commit is durable before any active row is removed.
                    with store.connect() as active:
                        for row in records:
                            active.execute(f'DELETE FROM {table} WHERE '+' AND '.join(k+'=?' for k in KEYS[table]),tuple(row[k] for k in KEYS[table]))
                    moved+=len(records)
                finally:db.close()
    return moved


def export_rows(store):
    with store.archive_lock:yield from _export_rows(store)


def _export_rows(store):
    def current():
        with store.connect() as db:
            for row in db.execute('SELECT * FROM observations ORDER BY observed_at DESC'):yield dict(row)
    def cold():
        directory=store.root/'archives'
        if not directory.exists():return
        if directory.is_symlink():raise ValueError('Invalid archive directory')
        for path in sorted(directory.glob('????-??.sqlite3'),reverse=True):
            if path.is_symlink():raise ValueError('Invalid archive file')
            db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=1);db.row_factory=sqlite3.Row
            try:
                for row in db.execute('SELECT * FROM observations ORDER BY observed_at DESC'):yield dict(row)
            finally:db.close()
    # At most two database readers are open, regardless of archive age.  A
    # crash after the cold commit but before the hot deletion leaves the same
    # exact row in both places; export it once, while preserving any row that
    # differs in a recorded field.
    seen=set();last_stamp=None;columns=TABLES['observations']
    for row in heapq.merge(current(),cold(),key=lambda row:row['observed_at'],reverse=True):
        if row['observed_at']!=last_stamp:
            seen.clear();last_stamp=row['observed_at']
        fingerprint=tuple(row.get(column) for column in columns)
        if fingerprint in seen:continue
        seen.add(fingerprint);yield row
