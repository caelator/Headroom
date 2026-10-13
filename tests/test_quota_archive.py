import sqlite3
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from aiusage.analytics import iso, utc_now
from aiusage.quota_archive import archive, connect, export_rows
from aiusage.store import Store


class QuotaArchiveTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        self.store=Store(self.root/'data');self.now=utc_now()
    def observation(self,age_days,window='window',remaining='80'):
        stamp=iso(self.now-timedelta(days=age_days))
        self.store.record_window('codex','synthetic',{'id':window,'label':'Example','status':'fresh',
            'used_pct':20,'remaining_pct':remaining,'observed_at':stamp,'window_minutes':300})
        return stamp
    def alert(self,stamp,alert_id='alert-1',message='Low'):
        with self.store.connect() as db:
            db.execute('INSERT INTO alerts(id,provider_id,window_id,kind,message,created_at) VALUES (?,?,?,?,?,?)',
                (alert_id,'codex','primary','low_remaining',message,stamp))
    def cold_rows(self,table):
        rows=[]
        for path in sorted((self.store.root/'archives').glob('*.sqlite3')):
            db=sqlite3.connect(path);db.row_factory=sqlite3.Row
            try:rows.extend(dict(row) for row in db.execute(f'SELECT * FROM {table}'))
            finally:db.close()
        return rows
    def test_archive_preserves_observations_and_alerts_and_hot_boundary(self):
        recent=self.observation(93)
        # record_window routes >93d rows only when an archive already exists;
        # direct hot rows model state written before retention was introduced.
        old=iso(self.now-timedelta(days=94))
        with self.store.connect() as db:
            db.execute("INSERT INTO observations VALUES ('codex','synthetic','old',?,NULL,300,'20','70','fresh','Example','test')",(old,))
        self.alert(old)
        self.assertEqual(archive(self.store,self.now),2)
        self.assertEqual([row['window_id'] for row in self.cold_rows('observations')],['old'])
        self.assertEqual(self.cold_rows('alerts')[0]['message'],'Low')
        with self.store.connect() as db:
            self.assertEqual([row[0] for row in db.execute('SELECT window_id FROM observations')],['window'])
            self.assertEqual(db.execute('SELECT count(*) FROM alerts').fetchone()[0],0)
        # Exactly at the 93-day cutoff remains hot (< cutoff is archived).
        self.assertEqual(recent,iso(self.now-timedelta(days=93)))
    def test_recurring_alert_id_preserves_each_creation(self):
        first=iso(self.now-timedelta(days=120));self.alert(first)
        self.assertEqual(archive(self.store,self.now),1)
        second=iso(self.now-timedelta(days=119));self.alert(second)
        self.assertEqual(archive(self.store,self.now),1)
        self.assertEqual(sorted(row['created_at'] for row in self.cold_rows('alerts')),sorted([first,second]))
        self.assertEqual({row['id'] for row in self.cold_rows('alerts')},{'alert-1'})
    def test_first_shape_alert_archive_migrates_without_losing_row(self):
        month=iso(self.now-timedelta(days=120))[:7];directory=self.store.root/'archives';directory.mkdir()
        path=directory/(month+'.sqlite3');db=sqlite3.connect(path)
        db.execute('CREATE TABLE alerts (id TEXT PRIMARY KEY,provider_id TEXT,window_id TEXT,kind TEXT,message TEXT,created_at TEXT)')
        first=iso(self.now-timedelta(days=120));db.execute('INSERT INTO alerts VALUES (?,?,?,?,?,?)',('alert-1','codex','primary','low_remaining','Low',first));db.commit();db.close()
        second=iso(self.now-timedelta(days=119));self.alert(second)
        self.assertEqual(archive(self.store,self.now),1)
        migrated=connect(path)
        try:self.assertEqual(migrated.execute('SELECT count(*) FROM alerts').fetchone()[0],2)
        finally:migrated.close()
    def test_crash_after_cold_commit_before_hot_delete_is_recoverable_and_exports_once(self):
        stamp=self.observation(1)
        # Put exact rows in cold and hot, as a crash in that interval leaves them.
        old=iso(self.now-timedelta(days=120))
        with self.store.connect() as db:
            db.execute("INSERT INTO observations VALUES ('codex','synthetic','old',?,NULL,300,'20','70','fresh','Example','test')",(old,))
        self.alert(old);self.assertEqual(archive(self.store,self.now),2)
        with self.store.connect() as db:
            db.execute("INSERT INTO observations VALUES ('codex','synthetic','old',?,NULL,300,'20','70','fresh','Example','test')",(old,))
            db.execute('INSERT INTO alerts VALUES (?,?,?,?,?,?)',('alert-1','codex','primary','low_remaining','Low',old))
        self.assertEqual([row['window_id'] for row in export_rows(self.store)].count('old'),1)
        self.assertEqual(archive(self.store,self.now),2)
        self.assertEqual(len(self.cold_rows('observations')),1);self.assertEqual(len(self.cold_rows('alerts')),1)
        with self.store.connect() as db:self.assertEqual(db.execute("SELECT count(*) FROM observations WHERE window_id='old'").fetchone()[0],0)
    def test_archive_conflict_preserves_hot_row(self):
        old=iso(self.now-timedelta(days=120));self.observation(1)
        with self.store.connect() as db:
            db.execute("INSERT INTO observations VALUES ('codex','synthetic','old',?,NULL,300,'20','70','fresh','Example','test')",(old,))
        self.assertEqual(archive(self.store,self.now),1)
        with self.store.connect() as db:
            db.execute("INSERT INTO observations VALUES ('codex','synthetic','old',?,NULL,300,'20','10','fresh','Example','test')",(old,))
        with self.assertRaises(ValueError):archive(self.store,self.now)
        with self.store.connect() as db:self.assertEqual(db.execute("SELECT remaining_pct FROM observations WHERE window_id='old'").fetchone()[0],'10')
    def test_export_holds_archive_until_reader_finishes(self):
        self.observation(1)
        old=iso(self.now-timedelta(days=120))
        with self.store.connect() as db:db.execute("INSERT INTO observations VALUES ('codex','synthetic','old',?,NULL,300,'20','70','fresh','Example','test')",(old,))
        rows=export_rows(self.store);self.assertEqual(next(rows)['window_id'],'window')
        done=[];thread=threading.Thread(target=lambda:done.append(archive(self.store,self.now)))
        thread.start();thread.join(timeout=.3);self.assertTrue(thread.is_alive());self.assertEqual(done,[])
        self.assertEqual(len(list(rows)),1);thread.join(timeout=5);self.assertFalse(thread.is_alive());self.assertEqual(done,[1])
        self.assertEqual(len(list(export_rows(self.store))),2)


if __name__=='__main__':unittest.main()
