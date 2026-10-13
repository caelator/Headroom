"""Accounting contract tests. Temp directories, temp SQLite files and synthetic JSONL only."""
import gzip
import json
import os
import shutil
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aiusage import router_telemetry  # noqa: E402
from aiusage.router_telemetry import Observer  # noqa: E402
from aiusage.local_models import FIELDS, Importer, Store, clean_text, integer, normalize_usage  # noqa: E402

PAD = 'p' * 300  # keeps the 256-byte checkpoint prefix of a growing file stable


def ts(n=0):
    return '2026-03-01T10:00:%02d.000Z' % n


def jsonl(*events):
    return b''.join(json.dumps(e).encode('utf-8') + b'\n' for e in events)


def usage(i, o, cached=None, reasoning=None):
    u = {'input_tokens': i, 'output_tokens': o, 'total_tokens': i + o}
    if cached is not None:
        u['input_tokens_details'] = {'cached_tokens': cached}
    if reasoning is not None:
        u['output_tokens_details'] = {'reasoning_tokens': reasoning}
    return u


U140 = usage(100, 40, cached=60, reasoning=10)


def session_meta(sid='sess-1', n=0, **extra):
    payload = {'id': sid, 'model_provider': 'openai', 'cwd': PAD}
    payload.update(extra)
    return {'timestamp': ts(n), 'type': 'session_meta', 'payload': payload}


def turn_context(model, turn='turn-1', n=0, **extra):
    payload = {'turn_id': turn, 'model': model}
    payload.update(extra)
    return {'timestamp': ts(n), 'type': 'turn_context', 'payload': payload}


def receipt(rid, u, model=None, n=0, ordinal=None, **extra):
    payload = {'usage': u}
    if rid:
        payload['response_id'] = rid
    if model:
        payload['model'] = model
    payload.update(extra)
    event = {'timestamp': ts(n), 'type': 'token_usage_record', 'payload': payload}
    if ordinal is not None:
        event['ordinal'] = ordinal
    return event


def token_count(total, last, n=0, **extra):
    info = {'total_token_usage': total, 'last_token_usage': last}
    info.update(extra)
    return {'timestamp': ts(n), 'type': 'event_msg', 'payload': {'type': 'token_count', 'info': info}}


def created(rid, model):
    return {'type': 'response.created', 'response': {'id': rid, 'model': model, 'status': 'in_progress'}}


def delta(text):
    return {'type': 'response.output_text.delta', 'delta': text}


def completed(rid, model, u):
    return {'type': 'response.completed',
            'response': {'id': rid, 'model': model, 'status': 'completed', 'usage': u}}


def sse_bytes(events, eol='\n'):
    return ''.join('event: %s%sdata: %s%s%s' % (e['type'], eol, json.dumps(e, ensure_ascii=False), eol, eol)
                   for e in events).encode('utf-8')


def split(body, size):
    return [body[i:i + size] for i in range(0, len(body), size)]


def run_observer(chunks, content_type='text/event-stream', encoding='', status=200,
                 model='gpt-alias', persist=False, headers=None, final_status=None):
    obs = Observer(model, headers, persist=persist)
    obs.response(status, content_type, encoding)
    for chunk in chunks:
        obs.feed(chunk)
    obs.finish(final_status)
    return obs


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='usage-test-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db_path = self.tmp / 'db' / 'usage.sqlite3'
        self.home = self.tmp / 'home'
        self.sessions = self.home / 'sessions'
        self.archive = self.home / 'archived_sessions'
        for directory in (self.sessions, self.archive):
            directory.mkdir(parents=True)
        self.store = Store(self.db_path)
        self.importer = Importer(self.store, self.home)
        patcher = mock.patch.object(router_telemetry, '_store', self.store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def put(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def rows(self, store=None):
        with (store or self.store).connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM events ORDER BY key')]

    def by_key(self, store=None):
        return {r['key']: r for r in self.rows(store)}

    def file_record(self, path, store=None):
        with (store or self.store).connect() as db:
            record = dict(db.execute('SELECT * FROM files WHERE path=?', (str(path),)).fetchone())
        record['state'] = json.loads(record['state'])
        return record

    def summary(self, store=None):
        return (store or self.store).summary([], 0)

    def models(self, store=None):
        return {m['model']: m for m in self.summary(store)['models']}

    def process(self, events, state=None, store=None):
        store = store or self.store
        importer = Importer(store, self.home)
        state = {} if state is None else state
        with store.lock, store.connect() as db:
            for event in events:
                importer.process_event(event, state, db)
        return state

    def assertRow(self, row, **expected):
        self.assertEqual({k: row[k] for k in expected}, expected)


class NormalizeUsageTests(unittest.TestCase):
    def test_non_dict_and_empty_usage_is_all_null(self):
        for raw in (None, [], 'usage', 7, True, {}):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_usage(raw), dict.fromkeys(FIELDS))

    def test_zero_is_a_value_and_missing_is_null(self):
        got = normalize_usage({'input_tokens': 0, 'output_tokens': 0})
        self.assertEqual((got['input_tokens'], got['output_tokens'], got['total_tokens']), (0, 0, 0))
        for key in ('cached_input_tokens', 'cache_write_input_tokens', 'reasoning_output_tokens'):
            self.assertIsNone(got[key], key)
        explicit = normalize_usage({'input_tokens': 4, 'output_tokens': 2, 'total_tokens': 6,
                                    'input_tokens_details': {'cached_tokens': 0},
                                    'output_tokens_details': {'reasoning_tokens': 0}})
        self.assertEqual(explicit['cached_input_tokens'], 0)
        self.assertEqual(explicit['reasoning_output_tokens'], 0)

    def test_total_is_derived_only_from_known_input_and_output(self):
        self.assertEqual(normalize_usage({'input_tokens': 7, 'output_tokens': 5})['total_tokens'], 12)
        self.assertIsNone(normalize_usage({'input_tokens': 7})['total_tokens'])
        self.assertIsNone(normalize_usage({'output_tokens': 7})['total_tokens'])
        only_total = normalize_usage({'total_tokens': 7})
        self.assertEqual(only_total['total_tokens'], 7)
        self.assertIsNone(only_total['input_tokens'])
        self.assertIsNone(only_total['output_tokens'])

    def test_prompt_completion_aliases_and_detail_subsets(self):
        got = normalize_usage({'prompt_tokens': 8, 'completion_tokens': 4,
                               'prompt_tokens_details': {'cached_tokens': 3},
                               'completion_tokens_details': {'reasoning_tokens': 1}})
        self.assertEqual((got['input_tokens'], got['output_tokens'], got['total_tokens']), (8, 4, 12))
        self.assertEqual((got['cached_input_tokens'], got['reasoning_output_tokens']), (3, 1))

    def test_detail_larger_than_its_parent_is_dropped(self):
        got = normalize_usage({'input_tokens': 5, 'output_tokens': 3,
                               'input_tokens_details': {'cached_tokens': 9},
                               'output_tokens_details': {'reasoning_tokens': 4}})
        self.assertIsNone(got['cached_input_tokens'])
        self.assertIsNone(got['reasoning_output_tokens'])
        self.assertEqual((got['input_tokens'], got['output_tokens'], got['total_tokens']), (5, 3, 8))

    def test_mismatched_total_discards_the_whole_record(self):
        got = normalize_usage({'input_tokens': 5, 'output_tokens': 3, 'total_tokens': 9,
                               'input_tokens_details': {'cached_tokens': 1}})
        self.assertEqual(got, dict.fromkeys(FIELDS))

    def test_invalid_total_is_never_trusted(self):
        for bad in (True, False, -8, 8.0, '8'):
            with self.subTest(bad=bad):
                got = normalize_usage({'input_tokens': 5, 'output_tokens': 3, 'total_tokens': bad})
                self.assertEqual(got['total_tokens'], 8)
        for bad in (True, False, -8, '8'):
            with self.subTest(alone=bad):
                self.assertIsNone(normalize_usage({'total_tokens': bad})['total_tokens'])

    def test_bool_negative_and_non_integer_counters_are_null(self):
        for bad in (True, False, -1, 1.0, '5', [5], {'n': 5}, 10 ** 15 + 1):
            with self.subTest(bad=bad):
                got = normalize_usage({'input_tokens': bad, 'output_tokens': 2})
                self.assertIsNone(got['input_tokens'])
                self.assertEqual(got['output_tokens'], 2)
                self.assertIsNone(got['total_tokens'])

    def test_invalid_detail_counters_are_null(self):
        for bad in (True, False, -3, 2.5, '1'):
            with self.subTest(bad=bad):
                got = normalize_usage({'input_tokens': 5, 'output_tokens': 3,
                                       'input_tokens_details': {'cached_tokens': bad},
                                       'output_tokens_details': {'reasoning_tokens': bad}})
                self.assertIsNone(got['cached_input_tokens'])
                self.assertIsNone(got['reasoning_output_tokens'])
                self.assertEqual((got['input_tokens'], got['output_tokens'], got['total_tokens']), (5, 3, 8))

    def test_integer_bounds(self):
        self.assertEqual(integer(0), 0)
        self.assertEqual(integer(10 ** 15), 10 ** 15)
        for bad in (10 ** 15 + 1, -1, True, False, None, 1.0, '1'):
            with self.subTest(bad=bad):
                self.assertIsNone(integer(bad))


class StoreAccountingTests(TempCase):
    def test_null_and_zero_stay_distinct(self):
        self.store.observe(dict(key='k:null', model='gpt-x', timestamp=ts(1)))
        self.store.observe(dict(key='k:zero', model='gpt-x', timestamp=ts(2), **dict.fromkeys(FIELDS, 0)))
        rows = self.by_key()
        for field in FIELDS:
            self.assertIsNone(rows['k:null'][field], field)
            self.assertEqual(rows['k:zero'][field], 0, field)
            self.assertIsNotNone(rows['k:zero'][field], field)
        totals = self.summary()['totals']
        self.assertEqual((totals['requests'], totals['unknown_usage'], totals['partial_usage'],
                          totals['total_tokens']), (2, 1, 1, 0))

    def test_exact_receipt_is_counted_once(self):
        events = [session_meta(), turn_context('gpt-5', n=1), receipt('resp_1', usage(10, 5, 4, 2), n=2)]
        self.process(events)
        self.process(events)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertRow(rows[0], key='r:resp_1', model='gpt-5', input_tokens=10, cached_input_tokens=4,
                       output_tokens=5, reasoning_output_tokens=2, total_tokens=15,
                       receipt=1, router=0, legacy=0, conflict=0)
        self.assertIsNone(rows[0]['cache_write_input_tokens'])
        totals = self.summary()['totals']
        self.assertEqual((totals['requests'], totals['input_tokens'], totals['cached_input_tokens'],
                          totals['total_tokens']), (1, 10, 4, 15))

    def test_conflicting_duplicate_response_is_flagged_not_summed(self):
        head = [session_meta(), turn_context('gpt-5')]
        self.process(head + [receipt('resp_1', usage(10, 5), n=1)])
        self.process(head + [receipt('resp_1', usage(11, 5), n=1)])
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['conflict'], 1)
        totals = self.summary()['totals']
        self.assertEqual((totals['requests'], totals['conflicts']), (1, 1))

    def test_missing_response_id_key_is_deterministic(self):
        events = [session_meta('sess-1'), turn_context('gpt-5'),
                  receipt(None, usage(10, 5), n=1, ordinal=1),
                  receipt(None, usage(10, 5), n=1, ordinal=2)]
        self.process(events)
        first = self.by_key()
        self.assertEqual(len(first), 2)
        for key, row in first.items():
            self.assertTrue(key.startswith('h:'), key)
            self.assertRow(row, response_id=None, receipt=1)

        self.process(events)
        self.assertEqual(set(self.by_key()), set(first))

        other = Store(self.tmp / 'other' / 'usage.sqlite3')
        self.process(events, store=other)
        self.assertEqual(set(self.by_key(other)), set(first))

        self.process([session_meta('sess-2'), turn_context('gpt-5'),
                      receipt(None, usage(10, 5), n=1, ordinal=1)])
        self.assertEqual(len(self.rows()), 3)
        self.assertEqual(self.summary()['totals']['total_tokens'], 45)

    def test_repeated_token_count_counters_are_not_added_again(self):
        state = self.process([
            session_meta(), turn_context('gpt-5'),
            token_count(usage(10, 5), usage(10, 5), n=1),
            token_count(usage(10, 5), usage(10, 5), n=2),
            token_count(usage(10, 5), usage(10, 5), n=1),
            token_count(usage(25, 10), usage(15, 5), n=3),
            token_count(usage(25, 10), usage(15, 5), n=4),
            token_count(usage(30, 12), usage(0, 0), n=5),
        ])
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertTrue(row['key'].startswith('l:'), row['key'])
            self.assertRow(row, legacy=1, receipt=0, router=0, source='legacy', model='gpt-5')
        summary = self.summary()
        totals = summary['totals']
        self.assertEqual((totals['input_tokens'], totals['output_tokens'], totals['total_tokens']), (25, 10, 35))
        self.assertEqual(summary['coverage'], dict(exact_receipts=0, legacy_estimates=2, router_requests=0))
        self.assertEqual(state.get('gaps', 0), 0)
        self.assertEqual(state['last_total'], 42)

    def test_receipt_covers_the_matching_token_count(self):
        self.process([session_meta(), turn_context('gpt-5'),
                      receipt('resp_1', usage(10, 5), n=1, thread_token_usage={'total_tokens': 15}),
                      token_count(usage(10, 5), usage(10, 5), n=1)])
        self.assertEqual([r['key'] for r in self.rows()], ['r:resp_1'])
        self.assertEqual(self.summary()['coverage'],
                         dict(exact_receipts=1, legacy_estimates=0, router_requests=0))
        self.assertEqual(self.summary()['totals']['total_tokens'], 15)

    def test_model_switch_in_turn_context_changes_attribution(self):
        self.process([
            session_meta(),
            turn_context('gpt-5', 'turn-1', n=1),
            token_count(usage(10, 5), usage(10, 5), n=2),
            turn_context('muse-spark', 'turn-2', n=3),
            token_count(usage(30, 10), usage(20, 5), n=4),
            receipt('resp_m', usage(7, 3), n=5),
            receipt('resp_x', usage(1, 1), model='gpt-explicit', n=6),
        ])
        models = self.models()
        self.assertEqual(models['gpt-5']['total_tokens'], 15)
        self.assertEqual(models['muse-spark']['total_tokens'], 35)
        self.assertEqual(models['muse-spark']['requests'], 2)
        self.assertEqual(models['gpt-explicit']['total_tokens'], 2)
        self.assertEqual(models['gpt-5']['provider'], 'OpenAI')
        self.assertEqual(models['muse-spark']['provider'], 'Meta')
        rows = self.rows()
        legacy = {r['model']: r for r in rows if r['legacy']}
        self.assertRow(legacy['gpt-5'], turn_id='turn-1', total_tokens=15)
        self.assertRow(legacy['muse-spark'], turn_id='turn-2', total_tokens=25)
        self.assertRow(self.by_key()['r:resp_m'], model='muse-spark', turn_id='turn-2')
        self.assertRow(self.by_key()['r:resp_x'], model='gpt-explicit')

    def test_mid_session_baseline_is_a_gap_not_usage(self):
        self.put(self.sessions / 'rollout.jsonl', jsonl(
            session_meta(), turn_context('gpt-5'),
            token_count(usage(100, 50), usage(10, 5), n=1),
            token_count(usage(110, 55), usage(10, 5), n=2)))
        self.importer.scan()
        summary = self.summary()
        self.assertEqual((summary['totals']['requests'], summary['totals']['total_tokens']), (2, 30))
        with self.store.connect() as db:
            gaps = json.loads(db.execute("SELECT value FROM metadata WHERE key='history_gaps'").fetchone()[0])
        self.assertEqual(gaps, 1)
        self.assertEqual(summary['import_']['files'], 1)
        self.assertEqual(summary['import_']['errors'], 0)
        self.assertIs(summary['import_']['scanning'], False)
        self.assertIsNotNone(summary['import_']['last_scan'])

    def test_copied_archive_and_fork_count_each_response_once(self):
        def history(sid, *extra):
            return jsonl(session_meta(sid), turn_context('gpt-5'),
                         receipt('resp_1', usage(10, 5), n=1), receipt('resp_2', usage(20, 10), n=2), *extra)

        original = history('sess-a')
        self.put(self.sessions / '2026' / '03' / '01' / 'rollout-a.jsonl', original)
        self.put(self.archive / 'rollout-a.jsonl', original)
        self.put(self.sessions / '2026' / '03' / '02' / 'rollout-fork.jsonl',
                 history('sess-fork', receipt('resp_3', usage(3, 3), n=3)))
        self.importer.scan()
        self.assertEqual(set(self.by_key()), {'r:resp_1', 'r:resp_2', 'r:resp_3'})
        summary = self.summary()
        totals = summary['totals']
        self.assertEqual((totals['requests'], totals['total_tokens'], totals['conflicts']), (3, 51, 0))
        self.assertEqual(summary['coverage']['exact_receipts'], 3)
        self.assertEqual(summary['import_']['files'], 3)

    def test_copied_legacy_history_is_not_double_counted(self):
        def history(sid):
            return jsonl(session_meta(sid), turn_context('gpt-5'),
                         token_count(usage(10, 5), usage(10, 5), n=1),
                         token_count(usage(25, 10), usage(15, 5), n=2))

        self.put(self.sessions / 'a.jsonl', history('sess-a'))
        self.put(self.archive / 'a.jsonl', history('sess-a'))
        self.put(self.sessions / 'fork.jsonl', history('sess-fork'))
        self.importer.scan()
        self.assertEqual(len(self.rows()), 2)
        totals = self.summary()['totals']
        self.assertEqual((totals['requests'], totals['total_tokens'], totals['conflicts']), (2, 35, 0))


class CheckpointTests(TempCase):
    def test_partial_record_waits_for_its_newline(self):
        path = self.sessions / 'rollout.jsonl'
        head = jsonl(session_meta(), turn_context('gpt-5'), token_count(usage(10, 5), usage(10, 5), n=1))
        tail = jsonl(token_count(usage(25, 10), usage(15, 5), n=2))
        self.put(path, head + tail[:40])
        self.importer.scan_file(path)
        self.assertEqual(len(self.rows()), 1)
        record = self.file_record(path)
        self.assertEqual((record['offset'], record['errors']), (len(head), 0))

        with path.open('ab') as handle:
            handle.write(tail[40:-1])
        self.importer.scan_file(path)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.file_record(path)['offset'], len(head))

        with path.open('ab') as handle:
            handle.write(tail[-1:])
        self.importer.scan_file(path)
        self.assertEqual(len(self.rows()), 2)
        record = self.file_record(path)
        self.assertEqual((record['offset'], record['errors']), (len(head) + len(tail), 0))
        self.assertEqual(record['state'].get('gaps', 0), 0)
        self.assertEqual(record['state']['last_total'], 35)
        self.assertEqual(self.summary()['totals']['total_tokens'], 35)

    def test_restart_resumes_from_the_durable_checkpoint(self):
        path = self.sessions / 'rollout.jsonl'
        head = jsonl(session_meta(), turn_context('gpt-5'), token_count(usage(10, 5), usage(10, 5), n=1))
        self.put(path, head)
        self.importer.scan_file(path)
        self.store.set_meta('started_at', '2026-03-01T00:00:00Z')
        before = self.rows()

        reopened = Store(self.db_path)
        importer = Importer(reopened, self.home)
        importer.scan_file(path)
        self.assertEqual(self.rows(reopened), before)

        with path.open('ab') as handle:
            handle.write(jsonl(token_count(usage(25, 10), usage(15, 5), n=2)))
        importer.scan_file(path)
        record = self.file_record(path, reopened)
        self.assertEqual(record['state'].get('gaps', 0), 0)
        self.assertEqual(record['state']['last_total'], 35)
        summary = reopened.summary([], 0)
        self.assertEqual((summary['totals']['requests'], summary['totals']['total_tokens']), (2, 35))
        self.assertEqual(summary['started_at'], '2026-03-01T00:00:00Z')

        Importer(Store(self.db_path), self.home).scan()
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.summary()['totals']['total_tokens'], 35)

    def test_scan_uses_bounded_connections_for_many_unchanged_files(self):
        for index in range(30):
            self.put(self.sessions / ('rollout-%02d.jsonl' % index), jsonl(
                session_meta('sess-%d' % index), turn_context('gpt-5'),
                receipt('resp_%d' % index, usage(10, 5), n=1)))
        self.importer.scan()
        before = self.rows()
        self.assertEqual(len(before), 30)

        with mock.patch.object(self.store, 'connect', wraps=self.store.connect) as connect:
            self.importer.scan()
            connection_calls = connect.call_count

        # The old scan opened SQLite once per unchanged file (here, 30 times)
        # in addition to its scan-level metadata work.  A bulk checkpoint read
        # keeps this independent of the number of unchanged files.
        self.assertLessEqual(connection_calls, 10)
        self.assertEqual(self.rows(), before)

    def test_scan_still_imports_appends_and_same_inode_prefix_rewrites(self):
        appended = self.sessions / 'appended.jsonl'
        rewritten = self.sessions / 'rewritten.jsonl'
        head = jsonl(session_meta('sess-a'), turn_context('gpt-5'))
        self.put(appended, head + jsonl(receipt('resp_before_append', usage(10, 5), n=1)))
        self.put(rewritten, head + jsonl(receipt('resp_before_rewrite', usage(10, 5), n=1)))
        self.importer.scan()
        self.assertEqual(set(self.by_key()), {'r:resp_before_append', 'r:resp_before_rewrite'})

        with appended.open('ab') as handle:
            handle.write(jsonl(receipt('resp_appended', usage(20, 5), n=2)))
        inode = rewritten.stat().st_ino
        replacement = jsonl(session_meta('sess-b'), turn_context('gpt-5'),
                            receipt('resp_rewritten_1', usage(11, 5), n=2),
                            receipt('resp_rewritten_2', usage(12, 5), n=3))
        rewritten.write_bytes(replacement)
        self.assertEqual(rewritten.stat().st_ino, inode)
        self.assertGreater(len(replacement), len(head))

        self.importer.scan()

        self.assertEqual(set(self.by_key()), {
            'r:resp_before_append', 'r:resp_before_rewrite',
            'r:resp_appended', 'r:resp_rewritten_1', 'r:resp_rewritten_2'})
        self.assertEqual(self.file_record(appended)['offset'], appended.stat().st_size)
        record = self.file_record(rewritten)
        self.assertEqual((record['offset'], record['state']['session_id']),
                         (rewritten.stat().st_size, 'sess-b'))

    def test_truncated_file_is_reread_from_the_start(self):
        path = self.sessions / 'rollout.jsonl'
        head = jsonl(session_meta(), turn_context('gpt-5'))
        first = head + jsonl(receipt('resp_old1', usage(10, 5), n=1), receipt('resp_old2', usage(20, 5), n=2))
        second = head + jsonl(receipt('resp_new', usage(1, 1), n=3))
        self.assertLess(len(second), len(first))
        self.put(path, first)
        self.importer.scan_file(path)
        self.assertEqual(self.file_record(path)['offset'], len(first))

        inode = path.stat().st_ino
        path.write_bytes(second)
        self.assertEqual(path.stat().st_ino, inode)
        self.importer.scan_file(path)
        self.assertEqual(set(self.by_key()), {'r:resp_old1', 'r:resp_old2', 'r:resp_new'})
        self.assertEqual(self.file_record(path)['offset'], len(second))

    def test_rotated_file_with_same_prefix_is_read_from_the_start(self):
        path = self.sessions / 'rollout.jsonl'
        head = jsonl(session_meta(), turn_context('gpt-5'))
        self.put(path, head + jsonl(receipt('resp_aaa', usage(10, 5), n=1)))
        self.importer.scan_file(path)
        before = self.file_record(path)
        old_inode = path.stat().st_ino

        os.rename(path, path.with_name('rollout.jsonl.1'))
        self.put(path, head + jsonl(receipt('resp_bbb', usage(11, 5), n=2), receipt('resp_ccc', usage(12, 5), n=3)))
        self.assertNotEqual(path.stat().st_ino, old_inode)
        self.assertGreater(path.stat().st_size, before['offset'])
        self.importer.scan_file(path)
        self.assertEqual(set(self.by_key()), {'r:resp_aaa', 'r:resp_bbb', 'r:resp_ccc'})
        after = self.file_record(path)
        self.assertNotEqual(after['inode'], before['inode'])
        self.assertEqual(after['offset'], path.stat().st_size)

    def test_in_place_rewrite_with_a_new_prefix_is_read_from_the_start(self):
        path = self.sessions / 'rollout.jsonl'
        first = jsonl(session_meta('sess-a'), turn_context('gpt-5'), receipt('resp_aaa', usage(10, 5), n=1))
        second = jsonl(session_meta('sess-b'), turn_context('gpt-5'),
                       receipt('resp_bbb', usage(11, 5), n=2), receipt('resp_ccc', usage(12, 5), n=3))
        self.put(path, first)
        self.importer.scan_file(path)

        inode = path.stat().st_ino
        path.write_bytes(second)
        self.assertEqual(path.stat().st_ino, inode)
        self.assertGreater(len(second), len(first))
        self.importer.scan_file(path)
        self.assertEqual(set(self.by_key()), {'r:resp_aaa', 'r:resp_bbb', 'r:resp_ccc'})
        self.assertEqual(self.file_record(path)['offset'], len(second))


class RouterMergeTests(TempCase):
    def via_router(self, store, alias, returned, rid):
        with mock.patch.object(router_telemetry, '_store', store):
            obs = Observer(alias, {'Session_Id': 'thread-1', 'X-Codex-Turn-Id': 'turn-1'})
            obs.data['attempts'] += 1
            obs.response(200, 'text/event-stream')
            obs.feed(sse_bytes([created(rid, returned), completed(rid, returned, U140)]))
            obs.finish()

    def deliver(self, order, alias, returned, rid='resp_real'):
        store = Store(self.tmp / order / 'usage.sqlite3')
        steps = [lambda: self.via_router(store, alias, returned, rid),
                 lambda: self.process([receipt(rid, U140, model=alias)], store=store)]
        for step in (steps if order == 'router_first' else reversed(steps)):
            step()
        return store

    def test_returned_model_overrides_alias_and_both_sources_merge_once(self):
        for order in ('router_first', 'receipt_first'):
            with self.subTest(order=order):
                store = self.deliver(order, 'gpt-alias', 'gpt-real')
                rows = self.rows(store)
                self.assertEqual([r['key'] for r in rows], ['r:resp_real'])
                self.assertRow(rows[0], model='gpt-real', requested_model='gpt-alias', status='completed',
                               http_status=200, attempts=1, response_id='resp_real',
                               thread_id='thread-1', turn_id='turn-1', receipt=1, router=1, legacy=0,
                               conflict=0, source='receipt+router', input_tokens=100,
                               cached_input_tokens=60, output_tokens=40, reasoning_output_tokens=10,
                               total_tokens=140)
                self.assertGreaterEqual(rows[0]['duration_ms'], 0)
                summary = self.summary(store)
                self.assertEqual((summary['totals']['requests'], summary['totals']['total_tokens']), (1, 140))
                self.assertEqual([m['model'] for m in summary['models']], ['gpt-real'])
                self.assertEqual(summary['coverage'],
                                 dict(exact_receipts=1, legacy_estimates=0, router_requests=1))
                self.assertEqual(summary['router']['active_requests'], 0)

    def test_provider_follows_the_returned_model(self):
        for order in ('router_first', 'receipt_first'):
            with self.subTest(order=order):
                store = self.deliver(order, 'auto-select', 'muse-real')
                self.assertRow(self.rows(store)[0], model='muse-real', requested_model='auto-select',
                               provider='Meta')
                self.assertEqual(self.models(store)['muse-real']['provider'], 'Meta')

    def test_router_failure_without_response_keeps_usage_unknown(self):
        self.store.start_request('req-f', 'gpt-5', 'thread-1', 'turn-1')
        started = self.summary()
        self.assertEqual(started['router']['active_requests'], 1)
        self.assertEqual(started['totals']['errors'], 0)

        self.store.finish_request('req-f', dict(status='upstream_error', http_status=502,
                                                attempts=2, duration_ms=7))
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertRow(rows[0], key='q:req-f', model='gpt-5', requested_model='gpt-5',
                       status='upstream_error', http_status=502, attempts=2, duration_ms=7,
                       response_id=None, router=1, receipt=0, source='router')
        for field in FIELDS:
            self.assertIsNone(rows[0][field], field)
        finished = self.summary()
        totals = finished['totals']
        self.assertEqual((totals['requests'], totals['unknown_usage'], totals['errors'],
                          totals['total_tokens']), (1, 1, 1, 0))
        self.assertEqual(finished['router']['active_requests'], 0)

    def test_observer_records_attempts_and_failure_without_inventing_usage(self):
        obs = Observer('gpt-5', {'session_id': 'thread-1'})
        self.assertEqual(self.summary()['router']['active_requests'], 1)
        obs.data['attempts'] += 1
        obs.data['attempts'] += 1
        obs.data['http_status'] = 502
        obs.finish('upstream_error')
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertRow(rows[0], key='q:' + obs.request_id, model='gpt-5', thread_id='thread-1',
                       status='upstream_error', http_status=502, attempts=2, router=1)
        self.assertIsNone(rows[0]['total_tokens'])
        totals = self.summary()['totals']
        self.assertEqual((totals['errors'], totals['unknown_usage'], totals['total_tokens']), (1, 1, 0))


class ObserverParsingTests(TempCase):
    def test_stream_survives_any_chunking_and_line_ending(self):
        model = 'modèle-日本'
        events = [created('resp_s', model), delta('héllo 日本語 🚀'), delta('more'),
                  completed('resp_s', model, usage(20, 8, cached=5, reasoning=3))]
        for eol in ('\n', '\r\n'):
            body = sse_bytes(events, eol)
            mid_accent = body.index('è'.encode()) + 1
            mid_emoji = body.index('🚀'.encode()) + 2
            chunkings = {'whole': [body], '64': split(body, 64), '7': split(body, 7),
                         'bytes': split(body, 1),
                         'mid-character': [body[:mid_accent], body[mid_accent:mid_emoji], body[mid_emoji:]]}
            for name, chunks in chunkings.items():
                with self.subTest(eol=repr(eol), chunking=name):
                    data = run_observer(chunks).data
                    self.assertEqual(data['model'], model)
                    self.assertEqual(data['requested_model'], 'gpt-alias')
                    self.assertEqual(data['response_id'], 'resp_s')
                    self.assertEqual(data['status'], 'completed')
                    self.assertEqual((data['input_tokens'], data['cached_input_tokens'],
                                      data['output_tokens'], data['reasoning_output_tokens'],
                                      data['total_tokens']), (20, 5, 8, 3, 28))
                    self.assertIsNone(data['cache_write_input_tokens'])

    def test_multiline_data_frames_ignore_comments_garbage_and_done(self):
        frame = json.dumps(completed('resp_ml', 'gpt-real', usage(6, 4)), indent=2)
        body = (b': keep-alive\n\nretry: 1000\n\ndata: not json\n\n' +
                b'event: response.completed\n' +
                b''.join(b'data: ' + line.encode() + b'\n' for line in frame.split('\n')) +
                b'\ndata: [DONE]\n\n')
        for name, chunks in (('whole', [body]), ('bytes', split(body, 1))):
            with self.subTest(chunking=name):
                data = run_observer(chunks).data
                self.assertEqual((data['status'], data['response_id'], data['model']),
                                 ('completed', 'resp_ml', 'gpt-real'))
                self.assertEqual((data['input_tokens'], data['output_tokens'], data['total_tokens']), (6, 4, 10))

    def test_final_frame_without_a_blank_line_is_still_used(self):
        body = sse_bytes([created('resp_t', 'gpt-real')]) + b'data: ' + json.dumps(
            completed('resp_t', 'gpt-real', usage(2, 1))).encode() + b'\n'
        data = run_observer([body]).data
        self.assertEqual((data['status'], data['total_tokens']), ('completed', 3))

    def test_terminal_status_events(self):
        cases = [
            ('failed', {'type': 'response.failed', 'response': {
                'id': 'resp_f', 'model': 'gpt-real', 'status': 'failed', 'usage': None}}, None),
            ('incomplete', {'type': 'response.incomplete', 'response': {
                'id': 'resp_f', 'model': 'gpt-real', 'usage': usage(3, 1)}}, 4),
            ('failed', {'type': 'error', 'message': 'boom'}, None),
        ]
        for expected, event, total in cases:
            with self.subTest(event=event['type']):
                data = run_observer([sse_bytes([created('resp_f', 'gpt-real'), event])]).data
                self.assertEqual(data['status'], expected)
                self.assertEqual(data.get('total_tokens'), total)

    def test_nonstream_json_plain_and_compressed(self):
        body = json.dumps({'id': 'resp_n', 'model': 'gpt-real', 'usage': usage(10, 5)}).encode()
        encodings = {'identity': ('', body), 'gzip': ('gzip', gzip.compress(body)),
                     'GZIP': ('GZIP', gzip.compress(body)), 'deflate': ('deflate', zlib.compress(body))}
        for name, (encoding, payload) in encodings.items():
            for size in (len(payload), 3, 1):
                with self.subTest(encoding=name, size=size):
                    data = run_observer(split(payload, size), content_type='application/json',
                                        encoding=encoding).data
                    self.assertEqual((data['status'], data['response_id'], data['model']),
                                     ('completed', 'resp_n', 'gpt-real'))
                    self.assertEqual((data['input_tokens'], data['output_tokens'], data['total_tokens']),
                                     (10, 5, 15))

    def test_compressed_stream(self):
        body = sse_bytes([created('resp_c', 'gpt-real'), delta('日本語'),
                          completed('resp_c', 'gpt-real', usage(9, 1))], '\r\n')
        for name, encoding, payload in (('gzip', 'gzip', gzip.compress(body)),
                                        ('deflate', 'deflate', zlib.compress(body))):
            with self.subTest(encoding=name):
                data = run_observer(split(payload, 5), encoding=encoding).data
                self.assertEqual((data['status'], data['response_id'], data['total_tokens']),
                                 ('completed', 'resp_c', 10))

    def test_corrupt_compressed_body_leaves_usage_unknown(self):
        data = run_observer([b'this is not gzip data'], content_type='application/json', encoding='gzip').data
        self.assertEqual(data['status'], 'interrupted')
        self.assertIsNone(data.get('total_tokens'))

    def test_nonstream_without_usage_is_not_marked_completed(self):
        body = json.dumps({'id': 'resp_n', 'model': 'gpt-real', 'usage': None}).encode()
        data = run_observer([body], content_type='application/json').data
        self.assertEqual((data['response_id'], data['model']), ('resp_n', 'gpt-real'))
        self.assertIsNone(data.get('total_tokens'))
        self.assertNotEqual(data['status'], 'completed')

    def test_finish_is_idempotent_and_explicit_status_wins(self):
        obs = run_observer([sse_bytes([created('resp_i', 'gpt-real')])], persist=True,
                           final_status='upstream_error')
        duration = obs.data['duration_ms']
        obs.finish()
        obs.finish('completed')
        self.assertEqual(obs.data['status'], 'upstream_error')
        self.assertEqual(obs.data['duration_ms'], duration)
        self.assertEqual(len(self.rows()), 1)
        self.assertRow(self.rows()[0], key='r:resp_i', status='upstream_error')

    def test_stream_without_terminal_event_is_interrupted_with_unknown_usage(self):
        body = (sse_bytes([created('resp_i', 'gpt-real'), delta('partial')]) +
                b'event: response.output_text.delta\ndata: {"type":"response.output_te')
        obs = run_observer([body], persist=True, headers={'session_id': 'thread-1'})
        self.assertEqual(obs.data['status'], 'interrupted')
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertRow(rows[0], key='r:resp_i', model='gpt-real', requested_model='gpt-alias',
                       status='interrupted', thread_id='thread-1', total_tokens=None)
        for field in FIELDS:
            self.assertIsNone(rows[0][field], field)
        totals = self.summary()['totals']
        self.assertEqual((totals['errors'], totals['unknown_usage'], totals['total_tokens']), (1, 1, 0))

    def test_connection_lost_before_any_body_keeps_request_key(self):
        obs = run_observer([], content_type='', persist=True)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertRow(rows[0], key='q:' + obs.request_id, model='gpt-alias', status='interrupted',
                       response_id=None, total_tokens=None)

    def test_http_error_is_failed_with_unknown_usage(self):
        body = json.dumps({'error': {'message': 'quota'}}).encode()
        obs = run_observer([body], content_type='application/json', status=429, persist=True)
        self.assertRow(self.rows()[0], key='q:' + obs.request_id, status='failed', http_status=429,
                       total_tokens=None)
        totals = self.summary()['totals']
        self.assertEqual((totals['errors'], totals['unknown_usage']), (1, 1))

    def test_explicit_zero_usage_is_known_not_unknown(self):
        zero = {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0,
                'input_tokens_details': {'cached_tokens': 0},
                'output_tokens_details': {'reasoning_tokens': 0}}
        run_observer([sse_bytes([created('resp_z', 'gpt-real'), completed('resp_z', 'gpt-real', zero)])],
                     persist=True)
        row = self.by_key()['r:resp_z']
        for field in ('input_tokens', 'cached_input_tokens', 'output_tokens',
                      'reasoning_output_tokens', 'total_tokens'):
            self.assertIsNotNone(row[field], field)
            self.assertEqual(row[field], 0, field)
        self.assertIsNone(row['cache_write_input_tokens'])
        totals = self.summary()['totals']
        self.assertEqual((totals['requests'], totals['unknown_usage'], totals['total_tokens']), (1, 0, 0))


SENTINELS = ('SENTINEL-PROMPT-8d41', 'SENTINEL-OUTPUT-17be', 'SENTINEL-BEARER-c09a',
             'SENTINEL-ERROR-52f0', 'SENTINEL-INSTRUCTIONS-aa71', 'SENTINEL-TOOL-93d2')


class PrivacyTests(TempCase):
    def test_text_fields_are_bounded_and_control_free(self):
        self.assertEqual(clean_text('a\nb', 'fb'), 'fb')
        self.assertEqual(clean_text('a\x00b', 'fb'), 'fb')
        self.assertEqual(clean_text(None, 'fb'), 'fb')
        self.assertEqual(clean_text(7, 'fb'), 'fb')
        self.assertEqual(clean_text('é'), 'é')
        self.assertEqual(len(clean_text('m' * 1000)), 240)
        self.assertEqual(Observer('gpt\n-injected', persist=False).model, 'unknown')

    def test_sentinels_never_reach_the_database(self):
        prompt, output, bearer, error, instructions, tool = SENTINELS
        path = self.sessions / 'rollout.jsonl'
        malformed = b'{"type": "event_msg", "payload": {"message": "' + prompt.encode() + b' broken\n'
        history = jsonl(
            session_meta('sess-p', instructions=instructions),
            turn_context('gpt-5', user_instructions=instructions),
            {'timestamp': ts(1), 'type': 'event_msg', 'payload': {'type': 'user_message', 'message': prompt}},
            {'timestamp': ts(2), 'type': 'response_item',
             'payload': {'type': 'message', 'content': [{'text': output}]}},
            {'timestamp': ts(3), 'type': 'response_item',
             'payload': {'type': 'function_call', 'arguments': tool}},
            receipt('resp_p', usage(10, 5), n=4, prompt=prompt, output=output,
                    authorization='Bearer ' + bearer),
            token_count(usage(25, 10), usage(15, 5), n=5, message=prompt)) + malformed
        self.put(path, history)
        for sentinel in (prompt, output, bearer, instructions, tool):
            self.assertIn(sentinel.encode(), history)
        self.importer.scan_file(path)
        self.assertEqual(self.file_record(path)['errors'], 1)

        headers = {'Authorization': 'Bearer ' + bearer, 'Cookie': 'session=' + bearer,
                   'session_id': 'thread-p', 'x-codex-turn-id': 'turn-p'}
        done = completed('resp_o', 'gpt-real', usage(3, 2))
        done['response']['output'] = [{'content': [{'text': output}]}]
        done['response']['instructions'] = instructions
        streamed = run_observer(
            [sse_bytes([created('resp_o', 'gpt-real'), delta(output),
                        {'type': 'response.output_item.done',
                         'item': {'type': 'message', 'content': [{'text': prompt}]}}, done])],
            persist=True, headers=headers)
        failed = run_observer([json.dumps({'error': {'message': error}}).encode()],
                              content_type='application/json', status=500, persist=True, headers=headers)
        cut_off = run_observer(
            [sse_bytes([created('resp_c', 'gpt-real'), delta(output)]) + b'data: {"delta": "' + output.encode()],
            persist=True, headers=headers)

        for obs in (streamed, failed, cut_off):
            self.assertEqual(obs.buffer, b'')
            self.assertEqual(obs.lines, [])
            for sentinel in SENTINELS:
                self.assertNotIn(sentinel, json.dumps(obs.data))
        self.assertEqual(len(self.rows()), 5)

        with self.store.connect() as db:
            dump = []
            for table in ('events', 'files', 'metadata'):
                dump.extend(tuple(r) for r in db.execute('SELECT * FROM ' + table))
        text = repr(dump) + json.dumps(self.summary())
        blobs = [p.read_bytes() for p in self.db_path.parent.iterdir() if p.is_file()]
        self.assertTrue(blobs)
        for sentinel in SENTINELS:
            with self.subTest(sentinel=sentinel):
                self.assertNotIn(sentinel, text)
                for blob in blobs:
                    self.assertNotIn(sentinel.encode(), blob)


if __name__ == '__main__':
    unittest.main()
