import json
from pathlib import Path
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import patch
from aiusage.analytics import utc_now,iso
from aiusage.store import Store

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name));self.now=utc_now()
    def snapshot(self,**extra):
        return {'id':'codex','status':'verified','account_ref':'account-a','observed_at':iso(self.now),
            'auth_method':'oauth','auth_verified':True,'verified_tier':'promax','source':'test',
            'windows':[{'id':'weekly','label':'Weekly','used_pct':90,'remaining_pct':10,
                'status':'fresh','observed_at':iso(self.now),'resets_at':iso(self.now+timedelta(days=1)),
                'window_minutes':10080,'source':'test'}], **extra}
    def codex(self):
        return next(p for p in self.store.portfolio()['providers'] if p['id']=='codex')
    def test_private_storage(self):
        self.assertEqual(self.store.root.stat().st_mode & 0o777,0o700)
        self.assertEqual(self.store.path.stat().st_mode & 0o777,0o600)
        self.assertEqual((self.store.root/'identity-salt').stat().st_mode & 0o777,0o600)
    def test_six_providers_and_unknown_not_full(self):
        p=self.store.portfolio();self.assertEqual(len(p['providers']),6)
        self.assertTrue(all(x['remaining_pct'] is None for x in p['providers']))
        self.assertFalse(any('_account_ref' in x for x in p['providers']))
    def test_fresh_measurement(self):
        self.store.save_provider(self.snapshot());p=self.codex()
        self.assertEqual(p['remaining_pct'],10);self.assertEqual(p['status'],'verified')
        self.assertIn('fresh_until',p['windows'][0])
    def test_exact_freshness_cutoff(self):
        self.store.save_provider(self.snapshot())
        with patch('aiusage.store.utc_now',return_value=self.now+timedelta(seconds=90)):
            p=self.codex()
        self.assertIsNone(p['remaining_pct']);self.assertIsNone(p['windows'][0]['remaining_pct'])
        self.assertEqual(p['windows'][0]['last_known_remaining_pct'],10)
        self.assertEqual(p['windows'][0]['velocity']['confidence'],'unavailable')
        self.assertEqual(p['status'],'stale')
    def test_reset_expiry(self):
        data=self.snapshot();data['windows'][0]['resets_at']=iso(self.now-timedelta(seconds=1))
        self.store.save_provider(data);p=self.codex();self.assertEqual(p['windows'][0]['status'],'expired')
        self.assertIsNone(p['remaining_pct'])
    def test_auth_failure_retains_only_stale_observation(self):
        self.store.save_provider(self.snapshot())
        self.store.save_provider({'id':'codex','status':'auth_required','observed_at':iso(),
            'auth_verified':False,'auth_method':'oauth','source':'test','windows':[],
            'error':{'code':'auth_required','message':'Sign in'}})
        p=self.codex();self.assertFalse(p['auth']['verified']);self.assertIsNone(p['remaining_pct'])
        self.assertEqual(p['windows'][0]['last_known_remaining_pct'],10)
        self.assertFalse(self.store.portfolio()['alerts'])
    def test_allowlist_drops_raw_profile_and_credentials(self):
        data=self.snapshot();data['raw_profile']={'email':'private@example.com'};data['token']='secret'
        data['windows'][0]['cookie']='secret';data['wallet']={'balance':10,'unit':'Credits','label':'Extra','token':'secret'}
        self.store.save_provider(data)
        with self.store.connect() as db:raw=db.execute("SELECT state FROM providers WHERE id='codex'").fetchone()[0]
        self.assertNotIn('secret',raw);self.assertNotIn('private@example',raw)
    def test_history_account_isolation_and_no_identity_exposure(self):
        self.store.save_provider(self.snapshot())
        self.store.save_provider(self.snapshot(account_ref='account-b'))
        history=self.store.history('codex');self.assertEqual(len(history['series'][0]['points']),1)
        self.assertNotIn('account-b',json.dumps(history))
    def test_duplicate_observations_are_idempotent(self):
        self.store.save_provider(self.snapshot());self.store.save_provider(self.snapshot())
        self.assertEqual(len(self.store.history('codex')['series'][0]['points']),1)
    def test_settings_validation(self):
        for value in (True,0,9,61,'30',None):
            with self.assertRaises(ValueError):self.store.save_settings({'refresh_interval_seconds':value})
        for val in ({'invalid':1},{'critical_remaining_pct':30,'warn_remaining_pct':20}):
            with self.assertRaises(ValueError):self.store.save_settings(val)
        self.assertEqual(self.store.save_settings({'refresh_interval_seconds':60})['refresh_interval_seconds'],60)
    def test_old_refresh_interval_tightened(self):
        with self.store.connect() as db:
            db.execute("UPDATE metadata SET value=? WHERE key='settings'",(json.dumps({'refresh_interval_seconds':300,'critical_remaining_pct':10,'warn_remaining_pct':25}),))
        self.assertEqual(self.store.settings()['refresh_interval_seconds'],30)
    def test_alerts_are_scoped_to_account(self):
        self.store.save_provider(self.snapshot());a=self.store.portfolio()['alerts'][0]['id']
        self.store.save_provider(self.snapshot(account_ref='account-b'));b=self.store.portfolio()['alerts'][0]['id']
        self.assertNotEqual(a,b)
    def test_future_observation_is_not_current(self):
        data=self.snapshot();data['windows'][0]['observed_at']=iso(self.now+timedelta(minutes=1))
        self.store.save_provider(data);self.assertIsNone(self.codex()['remaining_pct'])
    def test_unlimited_reading_expires(self):
        data=self.snapshot();data['windows'][0].update(status='unlimited',used_pct=None,remaining_pct=None)
        self.store.save_provider(data);self.assertEqual(self.codex()['status'],'verified')
        with patch('aiusage.store.utc_now',return_value=self.now+timedelta(seconds=90)):
            self.assertEqual(self.codex()['status'],'stale')
    def test_tier_verification_uses_profile_observation_time(self):
        stamp=iso(self.now-timedelta(minutes=4))
        self.store.save_provider(self.snapshot(tier_verified_at=stamp))
        self.assertEqual(self.codex()['plan']['verified_at'],stamp)
    def test_wallet_expires_with_source(self):
        self.store.save_provider(self.snapshot(wallet={'balance':100,'unit':'Credits','label':'Extra'}))
        self.assertIsNotNone(self.codex()['wallet'])
        with patch('aiusage.store.utc_now',return_value=self.now+timedelta(seconds=90)):
            self.assertIsNone(self.codex()['wallet'])

if __name__=='__main__':unittest.main()
