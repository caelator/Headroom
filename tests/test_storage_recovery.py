import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from unittest.mock import patch
from aiusage.analytics import iso,utc_now
from aiusage.store import Store,StorageError
from aiusage.quota_archive import export_rows


class StorageRecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        self.store=Store(self.root/'data')
    def snapshot(self,provider='codex'):
        return {'id':provider,'status':'verified','account_ref':'synthetic','auth_verified':True,
            'source':'test','observed_at':iso(),'windows':[{'id':'window','label':'Example','status':'fresh',
                'used_pct':20,'remaining_pct':80,'observed_at':iso(),'resets_at':iso(utc_now()+timedelta(hours=1))}]}
    def test_corrupt_provider_is_isolated_preserved_and_replaceable(self):
        self.store.save_provider(self.snapshot('claude'))
        with self.store.connect() as db:db.execute('INSERT INTO providers VALUES (?,?,?)',('codex','invalid-json',iso()))
        portfolio=self.store.portfolio();self.assertEqual(next(p for p in portfolio['providers'] if p['id']=='claude')['remaining_pct'],80)
        self.store.save_provider(self.snapshot())
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT state FROM providers WHERE id LIKE 'quarantined-codex-%'").fetchone()[0],'invalid-json')
        self.assertEqual(next(p for p in self.store.portfolio()['providers'] if p['id']=='codex')['remaining_pct'],80)
    def test_wrong_shape_and_missing_settings_are_repaired_without_erasing_original(self):
        with self.store.connect() as db:
            db.execute('INSERT INTO providers VALUES (?,?,?)',('codex','[]',iso()))
            db.execute("UPDATE metadata SET value=? WHERE key='settings'",(json.dumps({'refresh_interval_seconds':45,'warn_remaining_pct':60}),))
        again=Store(self.store.root)
        self.assertEqual(again.settings(),{'refresh_interval_seconds':45,'warn_remaining_pct':60,'critical_remaining_pct':10})
        with again.connect() as db:
            self.assertTrue(db.execute("SELECT 1 FROM providers WHERE id LIKE 'quarantined-codex-%'").fetchone())
            self.assertTrue(db.execute("SELECT 1 FROM metadata WHERE key LIKE 'quarantined-settings-%'").fetchone())
    def test_partial_error_keeps_only_labelled_last_known_values(self):
        self.store.save_provider(self.snapshot())
        self.store.save_provider({'id':'codex','status':'partial','windows':[], 'error':{'code':'no_quota','message':'Unavailable'}})
        p=next(p for p in self.store.portfolio()['providers'] if p['id']=='codex')
        self.assertIsNone(p['remaining_pct']);self.assertEqual(p['windows'][0]['last_known_remaining_pct'],80)
        self.assertEqual(p['windows'][0]['velocity']['confidence'],'unavailable')
    def test_read_snapshot_does_not_wait_for_writer_or_write_alerts(self):
        self.store.save_provider(self.snapshot())
        with self.store.connect() as writer:
            writer.execute('BEGIN IMMEDIATE');writer.execute("UPDATE metadata SET value=value WHERE key='settings'")
            start=time.monotonic();self.store.portfolio();self.assertLess(time.monotonic()-start,.5)
        with patch.object(self.store,'connect',wraps=self.store.connect) as connections:self.store.portfolio()
        self.assertEqual(connections.call_count,1)
    def test_short_salt_is_rejected_and_not_replaced(self):
        salt=self.store.root/'identity-salt';salt.write_bytes(b'short')
        with self.assertRaises(StorageError):Store(self.store.root)
        self.assertEqual(salt.read_bytes(),b'short')
    def test_concurrent_first_starts_share_one_complete_salt(self):
        root=self.root/'parallel';salts=[];errors=[]
        def create():
            try:salts.append(Store(root).salt)
            except Exception as error:errors.append(error)
        threads=[threading.Thread(target=create) for _ in range(3)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(timeout=5)
        self.assertEqual(errors,[]);self.assertEqual(len(set(salts)),1);self.assertEqual(len(salts[0]),32)
    def test_hot_retention_archives_all_rows_and_export_remains_complete(self):
        now=utc_now()
        for age in (89,93,94,120):
            self.store.record_window('codex','synthetic',{'id':str(age),'label':'Example','status':'fresh',
                'used_pct':20,'remaining_pct':80,'observed_at':iso(now-timedelta(days=age))})
        self.assertEqual(self.store.maintenance(now),2)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM observations').fetchone()[0],2)
        rows=list(export_rows(self.store));self.assertEqual(len(rows),4)
        self.assertEqual(rows,sorted(rows,key=lambda row:row['observed_at'],reverse=True))
        self.assertEqual(self.store.maintenance(now),0)
        self.store.record_window('codex','synthetic',{'id':'120','label':'Example','status':'fresh',
            'used_pct':20,'remaining_pct':80,'observed_at':iso(now-timedelta(days=120))})
        self.assertEqual(len(list(export_rows(self.store))),4)
    def test_archive_failure_never_deletes_active_history(self):
        self.store.record_window('codex','synthetic',{'id':'old','label':'Example','status':'fresh','observed_at':iso(utc_now()-timedelta(days=120))})
        with patch('aiusage.quota_archive.connect',side_effect=OSError('disk')):
            with self.assertRaises(OSError):self.store.maintenance()
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM observations').fetchone()[0],1)
    def test_unsupported_schema_and_corrupt_database_are_preserved(self):
        with self.store.connect() as db:db.execute('PRAGMA user_version=999')
        with self.assertRaises(StorageError):Store(self.store.root)
        damaged=self.root/'damaged';damaged.mkdir();(damaged/'plans.sqlite3').write_bytes(b'not-a-database')
        with self.assertRaises(sqlite3.DatabaseError):Store(damaged)
        self.assertEqual((damaged/'plans.sqlite3').read_bytes(),b'not-a-database')


    def test_blank_wallet_error_and_labels_are_quarantined_with_original_bytes(self):
        valid={'id':'codex','status':'verified','account_ref':'synthetic','source':'test','observed_at':iso(),
            'windows':[{'id':'window','label':'Example','status':'fresh','used_pct':20,'remaining_pct':80}]}
        cases=[]
        for change in ({'wallet':{'balance':'','unit':'credits','label':'Wallet'}},
                       {'wallet':{'balance':10,'unit':' ','label':'Wallet'}},
                       {'error':{'code':'','message':'Failed'}},
                       {'error':{'code':'failed','message':' '}},
                       {'windows':[{'id':'window','label':' ','status':'fresh'}]},
                       {'windows':[{'id':' ','label':'Example','status':'fresh'}]}):
            state=dict(valid);state.update(change);cases.append(json.dumps(state))
        with self.store.connect() as db:db.execute("UPDATE metadata SET value=? WHERE key='settings'",(json.dumps({'refresh_interval_seconds':45,'warn_remaining_pct':60,'critical_remaining_pct':10}),))
        for raw in cases:
            with self.store.connect() as db:
                db.execute("DELETE FROM providers WHERE id='codex' OR id LIKE 'quarantined-codex-%'")
                db.execute('INSERT INTO providers(id,state,last_attempt) VALUES (?,?,?)',('codex',raw,iso()))
            again=Store(self.store.root);self.assertEqual(again.settings()['refresh_interval_seconds'],45)
            with again.connect() as db:
                preserved=db.execute("SELECT state FROM providers WHERE id LIKE 'quarantined-codex-%'").fetchall()
                self.assertEqual([row[0] for row in preserved],[raw])
                self.assertFalse(db.execute("SELECT 1 FROM metadata WHERE key LIKE 'quarantined-settings-%'").fetchone())
    def test_same_malformed_state_can_be_quarantined_repeatedly(self):
        raw='{"id":"codex","status":"verified","windows":"bad"}'
        for _ in range(2):
            with self.store.connect() as db:db.execute('INSERT INTO providers(id,state,last_attempt) VALUES (?,?,?)',('codex',raw,iso()))
            Store(self.store.root)
        with self.store.connect() as db:
            rows=db.execute("SELECT state FROM providers WHERE id LIKE 'quarantined-codex-%'").fetchall()
        self.assertEqual([row[0] for row in rows],[raw,raw])
    def test_save_provider_normalizes_blank_window_wallet_and_error(self):
        data=self.snapshot();data['windows'][0]['label']=' ';data['wallet']={'balance':' ','unit':'credits','label':'Wallet'}
        data['error']={'code':' ','message':' '}
        self.store.save_provider(data)
        with self.store.connect() as db:raw=json.loads(db.execute("SELECT state FROM providers WHERE id='codex'").fetchone()[0])
        self.assertEqual(raw['windows'][0]['label'],'window');self.assertIsNone(raw['wallet'])
        self.assertTrue(raw['error']['code']);self.assertTrue(raw['error']['message'])
        self.assertEqual(next(p for p in self.store.portfolio()['providers'] if p['id']=='codex')['remaining_pct'],80)
    def test_incomplete_startup_schema_is_preserved(self):
        root=self.root/'incomplete';root.mkdir();db=sqlite3.connect(root/'plans.sqlite3')
        db.executescript("""CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE providers(id TEXT PRIMARY KEY,state TEXT NOT NULL);
        CREATE TABLE observations(provider_id TEXT NOT NULL,account_ref TEXT NOT NULL,window_id TEXT NOT NULL,observed_at TEXT NOT NULL,resets_at TEXT,window_minutes INTEGER,used_pct TEXT,remaining_pct TEXT,status TEXT NOT NULL,label TEXT NOT NULL,source TEXT NOT NULL);
        CREATE TABLE imports(path TEXT PRIMARY KEY,inode TEXT NOT NULL,offset INTEGER NOT NULL,state TEXT NOT NULL);
        CREATE TABLE alerts(id TEXT PRIMARY KEY,provider_id TEXT NOT NULL,window_id TEXT NOT NULL,kind TEXT NOT NULL,message TEXT NOT NULL,created_at TEXT NOT NULL);""")
        db.execute('INSERT INTO providers VALUES (?,?)',('codex','original-state'));db.commit();db.close()
        with self.assertRaises(StorageError):Store(root)
        db=sqlite3.connect(root/'plans.sqlite3');self.assertEqual(db.execute('SELECT state FROM providers').fetchone()[0],'original-state');db.close()
    def test_superset_startup_schema_remains_usable(self):
        root=self.root/'superset';root.mkdir();db=sqlite3.connect(root/'plans.sqlite3')
        db.executescript("""CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL,extra TEXT);
        CREATE TABLE providers(id TEXT PRIMARY KEY,state TEXT NOT NULL,last_attempt TEXT NOT NULL,extra TEXT);
        CREATE TABLE observations(provider_id TEXT NOT NULL,account_ref TEXT NOT NULL,window_id TEXT NOT NULL,observed_at TEXT NOT NULL,resets_at TEXT,window_minutes INTEGER,used_pct TEXT,remaining_pct TEXT,status TEXT NOT NULL,label TEXT NOT NULL,source TEXT NOT NULL,extra TEXT,PRIMARY KEY(provider_id,account_ref,window_id,observed_at));
        CREATE TABLE imports(path TEXT PRIMARY KEY,inode TEXT NOT NULL,offset INTEGER NOT NULL,state TEXT NOT NULL,extra TEXT);
        CREATE TABLE alerts(id TEXT PRIMARY KEY,provider_id TEXT NOT NULL,window_id TEXT NOT NULL,kind TEXT NOT NULL,message TEXT NOT NULL,created_at TEXT NOT NULL,extra TEXT);""")
        db.commit();db.close();store=Store(root);store.save_provider(self.snapshot())
        self.assertEqual(next(p for p in store.portfolio()['providers'] if p['id']=='codex')['remaining_pct'],80)

if __name__=='__main__':unittest.main()
