import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
import threading
from aiusage.router_telemetry import Observer
from aiusage.local_models import Store, normalize_usage


class Regressions(unittest.TestCase):
    def test_summary_reads_do_not_wait_for_importer_mutex(self):
        with tempfile.TemporaryDirectory() as root:
            s = Store(Path(root) / 'db')
            s.observe(dict(key='r:readable', model='gpt-real', total_tokens=10))
            completed = threading.Event()
            errors = []
            def read():
                try:
                    self.assertEqual(s.summary([], 0)['totals']['total_tokens'], 10)
                except Exception as error:
                    errors.append(error)
                finally:
                    completed.set()
            with s.lock:
                thread = threading.Thread(target=read)
                thread.start()
                ready = completed.wait(1)
            thread.join(timeout=2)
            self.assertTrue(ready, 'Reader was blocked by the importer mutex')
            self.assertEqual(errors, [])

    def test_untyped_stream_one_byte_at_a_time(self):
        o = Observer('gpt-test', persist=False)
        o.response(200, '')
        wire = b'event: response.completed\r\ndata: {"type":"response.completed","response":{"id":"r","model":"gpt-test","usage":{"input_tokens":8,"output_tokens":2,"total_tokens":10}}}\r\n\r\n'
        for b in wire: o.feed(bytes([b]))
        o.finish()
        self.assertEqual(o.data['status'], 'completed')
        self.assertEqual(o.data['total_tokens'], 10)

    def test_cache_write_details_are_known(self):
        self.assertEqual(normalize_usage({'input_tokens': 10, 'output_tokens': 3,
            'input_tokens_details': {'cached_tokens': 5, 'cache_write_tokens': 2}})['cache_write_input_tokens'], 2)

    def test_duplicate_unknown_model_cannot_erase_attribution(self):
        with tempfile.TemporaryDirectory() as root:
            s = Store(Path(root) / 'db')
            s.observe(dict(key='r:stable', model='gpt-real', receipt=1, input_tokens=7, output_tokens=3, total_tokens=10))
            s.observe(dict(key='r:stable', model='unknown', receipt=1, input_tokens=7, output_tokens=3, total_tokens=10))
            d = s.summary([], 0)
            self.assertEqual(d['totals']['requests'], 1)
            self.assertEqual(d['models'][0]['model'], 'gpt-real')

    def test_duplicate_conflicting_receipts_keep_first_and_flag(self):
        with tempfile.TemporaryDirectory() as root:
            s = Store(Path(root) / 'db')
            s.observe(dict(key='r:stable', model='gpt-real', receipt=1, input_tokens=7, output_tokens=3, total_tokens=10))
            s.observe(dict(key='r:stable', model='gpt-other', receipt=1, input_tokens=8, output_tokens=3, total_tokens=11))
            d = s.summary([], 0)
            self.assertEqual(d['totals']['total_tokens'], 10)
            self.assertEqual(d['totals']['conflicts'], 1)
            self.assertEqual(d['models'][0]['model'], 'gpt-real')

    def test_idless_provider_join_in_both_orders(self):
        for receipt_first in (False, True):
            with self.subTest(receipt_first=receipt_first), tempfile.TemporaryDirectory() as root:
                s = Store(Path(root) / 'db')
                s.start_request('q', 'muse-model', 'thread')
                receipt = dict(key='r:client-generated', model='muse-model',
                               thread_id='thread', response_id='client-generated', receipt=1,
                               input_tokens=7, output_tokens=3, total_tokens=10)
                end = dict(model='muse-model', input_tokens=7, output_tokens=3,
                           total_tokens=10, status='completed', duration_ms=1)
                if receipt_first: s.observe(receipt)
                s.finish_request('q', end)
                if not receipt_first: s.observe(receipt)
                d = s.summary([], 0)
                self.assertEqual(d['totals']['requests'], 1)
                self.assertEqual(d['totals']['total_tokens'], 10)
                self.assertEqual(d['coverage']['router_requests'], 1)
                self.assertEqual(d['coverage']['exact_receipts'], 1)
                self.assertIn('matched', d['models'][0]['source'])

    def test_daily_buckets_follow_bogota(self):
        with tempfile.TemporaryDirectory() as root:
            s = Store(Path(root) / 'db')
            s.observe(dict(key='r:date', model='gpt-date', timestamp='2026-10-12T03:00:00Z', total_tokens=10))
            self.assertEqual(s.summary([], 0)['daily'][0]['day'], '2026-10-11')

    def test_unparsed_wire_can_merge_unique_same_thread_receipt(self):
        with tempfile.TemporaryDirectory() as root:
            s = Store(Path(root) / 'db')
            s.observe(dict(key='q:missed', model='gpt-real', thread_id='t', router=1,
                           timestamp='2026-10-12T12:00:00Z', duration_ms=1000,
                           status='interrupted', http_status=200))
            s.observe(dict(key='r:known', response_id='known', model='gpt-real', thread_id='t',
                           receipt=1, timestamp='2026-10-12T12:00:01.010Z',
                           input_tokens=7, output_tokens=3, total_tokens=10))
            s.reconcile_unparsed()
            d = s.summary([], 0)
            self.assertEqual(d['totals']['requests'], 1)
            self.assertEqual(d['totals']['unknown_usage'], 0)
            self.assertIn('matched', d['models'][0]['source'])

    def test_unparsed_wire_ambiguous_receipts_stay_separate(self):
        with tempfile.TemporaryDirectory() as root:
            s = Store(Path(root) / 'db')
            s.observe(dict(key='q:missed', model='gpt-real', thread_id='t', router=1,
                           timestamp='2026-10-12T12:00:00Z', duration_ms=1000,
                           status='interrupted', http_status=200))
            for rid in ('a', 'b'):
                s.observe(dict(key='r:' + rid, response_id=rid, model='gpt-real', thread_id='t',
                               receipt=1, timestamp='2026-10-12T12:00:01.010Z', total_tokens=10))
            s.reconcile_unparsed()
            self.assertEqual(s.summary([], 0)['totals']['requests'], 3)


if __name__ == '__main__': unittest.main()
