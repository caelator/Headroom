import tempfile
import unittest
import threading
import time
from unittest.mock import patch
from aiusage.collectors import Collectors
from aiusage.store import Store
from aiusage.analytics import iso

class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.c=Collectors(Store(self.temp.name),isolated=False)
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

    def test_built_in_adapters_receive_store_and_preserve_account_scope(self):
        for provider in ('codex','cursor','zai','antigravity'):
            result={'id':provider,'status':'partial','observed_at':iso(),
                    'account_ref':'synthetic-account','source':provider+'_direct','windows':[]}
            with self.subTest(provider=provider),patch('aiusage.collectors.collect_'+provider,return_value=result) as adapter:
                self.c.fetch(provider)
                adapter.assert_called_once_with(self.c.store)
                portfolio=self.c.store.portfolio()
                saved=next(p for p in portfolio['providers'] if p['id']==provider)
                self.assertEqual(saved['source'],provider+'_direct')

    def test_no_identity_never_inherits_configured_credentials(self):
        window={'id':'primary','label':'Example','status':'fresh','used_pct':40,
                'remaining_pct':60,'observed_at':iso(),'source':'codex_direct'}
        result={'id':'codex','status':'verified','observed_at':iso(),'windows':[window],
                'account_ref':None,'source':'codex_direct'}
        with patch('aiusage.collectors.collect_codex',return_value=result):self.c.fetch('codex')
        saved=next(p for p in self.c.store.portfolio()['providers'] if p['id']=='codex')
        self.assertEqual(saved['status'],'partial')
        self.assertEqual(saved['error']['code'],'identity_unverified')
        self.assertEqual(saved['windows'][0]['velocity']['confidence'],'unavailable')
    def test_refresh_does_not_bypass_rate_limit_deadline(self):
        self.c.next_allowed['codex']=time.monotonic()+60
        with patch.object(self.c,'fetch') as fetch:
            thread=threading.Thread(target=self.c.run,args=('codex',));thread.start()
            self.c.request_refresh();time.sleep(.03);self.c.stop.set();thread.join(timeout=1)
        fetch.assert_not_called();self.assertFalse(thread.is_alive())
    def test_storage_failure_does_not_kill_collector_owner(self):
        attempted=threading.Event()
        def fail(provider):attempted.set();self.c.stop.set();raise OSError('private-sentinel')
        with patch.object(self.c,'fetch',side_effect=fail):
            thread=threading.Thread(target=self.c.run,args=('codex',));thread.start()
            self.assertTrue(attempted.wait(1));self.c.events['codex'].set();thread.join(timeout=1)
        self.assertFalse(thread.is_alive());self.assertEqual(self.c.busy,set())

if __name__=='__main__':unittest.main()
