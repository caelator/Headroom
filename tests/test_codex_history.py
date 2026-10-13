import json
from pathlib import Path
import tempfile
import unittest
import sqlite3
from unittest.mock import patch
from aiusage.codex_history import CodexQuotaHistory
from aiusage.store import Store
from aiusage.analytics import iso,utc_now


def records(account,used=20):
    values=[{'type':'session_meta','payload':{'creator_account_id':account,'padding':'x'*300}},
        {'type':'turn_context','payload':{'model':'gpt-test'}},
        {'type':'event_msg','timestamp':iso(),'payload':{'type':'token_count','rate_limits':{'limit_id':'codex',
            'primary':{'used_percent':used,'window_minutes':300,'resets_at':utc_now().timestamp()+3600}}}}]
    return ''.join(json.dumps(value)+'\n' for value in values)


class QuotaHistoryTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        self.store=Store(self.root/'data');self.importer=CodexQuotaHistory(self.store,self.root)
    def test_same_inode_rewrite_does_not_reuse_account_context(self):
        file=self.root/'session.jsonl';file.write_text(records('first'))
        ref=self.store.fingerprint('codex|first');self.importer.scan_file(file,ref)
        file.write_text(records('other',40)+' '*100+'\n')
        self.importer.scan_file(file,ref)
        with self.store.connect() as db:
            rows=db.execute('SELECT count(*) FROM observations').fetchone()[0]
        self.assertEqual(rows,1)
    def test_unchanged_file_deduplicates_and_partial_lines_wait(self):
        file=self.root/'session.jsonl';wire=records('first');file.write_text(wire[:-1])
        ref=self.store.fingerprint('codex|first');self.importer.scan_file(file,ref)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM observations').fetchone()[0],0)
        with file.open('a') as stream:stream.write('\n')
        self.importer.scan_file(file,ref);self.importer.scan_file(file,ref)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM observations').fetchone()[0],1)
    def test_one_database_failure_does_not_skip_independent_files(self):
        folder=self.root/'sessions';folder.mkdir()
        for name in ('first.jsonl','second.jsonl'):(folder/name).write_text(records('first'))
        with patch('aiusage.codex_history.codex_account_id',return_value='first'),patch.object(self.importer,'scan_file',side_effect=[sqlite3.OperationalError('failure'),None]) as scan:
            self.importer.scan()
        self.assertEqual(scan.call_count,2)

    def test_bulk_scan_unchanged_files_uses_one_checkpoint_connection(self):
        folder=self.root/'sessions';folder.mkdir()
        for n in range(30):(folder/f'{n}.jsonl').write_text(records('first'))
        with patch('aiusage.codex_history.codex_account_id',return_value='first'):
            self.importer.scan()
            with patch.object(self.store,'connect',wraps=self.store.connect) as connect:
                self.importer.scan()
            self.assertEqual(connect.call_count,1)
            # An append still enters the write path and imports the new quota.
            file=folder/'0.jsonl'
            with file.open('a') as stream:stream.write(records('first',41))
            self.importer.scan()
            with self.store.connect() as db:
                self.assertTrue(db.execute("SELECT 1 FROM observations WHERE used_pct='41' OR used_pct='41.0'").fetchone())
            # A same-inode prefix rewrite is checked even through bulk preflight.
            file.write_text(records('other',62)+' '*100+'\n');self.importer.scan()
            with self.store.connect() as db:
                self.assertFalse(db.execute("SELECT 1 FROM observations WHERE used_pct='62' OR used_pct='62.0'").fetchone())


    def test_authenticated_global_slots_write_duration_qualified_ids_only(self):
        values=[{'type':'session_meta','payload':{'creator_account_id':'first'}},
            {'type':'turn_context','payload':{'model':'gpt-test'}}]
        def event(minutes,used,slot='primary',limit='codex',stamp=None):
            return {'type':'event_msg','timestamp':stamp or iso(),'payload':{'type':'token_count','rate_limits':{'limit_id':limit,
                slot:{'used_percent':used,'window_minutes':minutes,'resets_at':utc_now().timestamp()+3600}}}}
        values.extend([event(300,20),event(10080,30,'secondary'),event(60,40),event(300,50,limit='codex-spark')])
        file=self.root/'session.jsonl';file.write_text(''.join(json.dumps(value)+'\n' for value in values))
        ref=self.store.fingerprint('codex|first');self.importer.scan_file(file,ref)
        with self.store.connect() as db:rows=db.execute('SELECT window_id,window_minutes FROM observations ORDER BY window_id').fetchall()
        self.assertEqual([(row[0],row[1]) for row in rows],[('primary-300m',300),('secondary-10080m',10080)])
    def test_duration_not_slot_names_legacy_window(self):
        values=[{'type':'session_meta','payload':{'creator_account_id':'first'}},{'type':'turn_context','payload':{'model':'gpt-test'}},
            {'type':'event_msg','timestamp':iso(),'payload':{'type':'token_count','rate_limits':{'limit_id':'codex',
                'primary':{'used_percent':20,'window_minutes':10080,'resets_at':utc_now().timestamp()+3600},
                'secondary':{'used_percent':30,'window_minutes':300,'resets_at':utc_now().timestamp()+3600}}}}]
        file=self.root/'session.jsonl';file.write_text(''.join(json.dumps(value)+'\n' for value in values))
        self.importer.scan_file(file,self.store.fingerprint('codex|first'))
        with self.store.connect() as db:rows=db.execute('SELECT window_id FROM observations ORDER BY window_id').fetchall()
        self.assertEqual([row[0] for row in rows],['primary-300m','secondary-10080m'])

if __name__=='__main__':unittest.main()
