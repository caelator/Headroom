import tempfile
import unittest
from unittest.mock import patch
from aiusage.collectors import Collectors
from aiusage.store import Store
from aiusage.analytics import iso

class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.c=Collectors(Store(self.temp.name))
    def test_rate_limit_backoff_is_independent_and_clears_on_success(self):
        failure={'id':'claude','status':'error','observed_at':iso(),'windows':[],
            'error':{'code':'rate_limited','message':'Retrying'},'retry_after_seconds':60}
        with patch('aiusage.collectors.collect_claude',return_value=failure):
            self.c.fetch('claude');self.assertEqual(self.c.retry_delay['claude'],60)
            self.c.fetch('claude');self.assertEqual(self.c.retry_delay['claude'],120)
        self.assertEqual(self.c.retry_delay['codex'],0)
        with patch('aiusage.collectors.collect_claude',return_value={'id':'claude','status':'partial','observed_at':iso(),'windows':[]}):
            self.c.fetch('claude')
        self.assertEqual(self.c.retry_delay['claude'],0)
    def test_refresh_wakes_all_six_independent_collectors(self):
        self.c.request_refresh();self.assertEqual(len(self.c.events),6)
        self.assertTrue(all(e.is_set() for e in self.c.events.values()))
    def test_synchronizing_reflects_actual_busy_set(self):
        self.assertFalse(self.c.synchronizing);self.c.busy.add('muse');self.assertTrue(self.c.synchronizing)

if __name__=='__main__':unittest.main()
