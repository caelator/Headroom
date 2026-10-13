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
    def persisted_alerts(self,provider=None):
        with self.store.connect() as db:
            if provider is None:rows=db.execute('SELECT id,provider_id,window_id,kind FROM alerts ORDER BY provider_id,kind').fetchall()
            else:rows=db.execute('SELECT id,provider_id,window_id,kind FROM alerts WHERE provider_id=? ORDER BY kind',(provider,)).fetchall()
        return [dict(row) for row in rows]
    def test_private_storage(self):
        self.assertEqual(self.store.root.stat().st_mode & 0o777,0o700)
        self.assertEqual(self.store.path.stat().st_mode & 0o777,0o600)
        self.assertEqual((self.store.root/'identity-salt').stat().st_mode & 0o777,0o600)
    def test_six_providers_and_unknown_not_full(self):
        p=self.store.portfolio();self.assertEqual(len(p['providers']),6)
        self.assertTrue(all(x['remaining_pct'] is None for x in p['providers']))
        self.assertFalse(any('_account_ref' in x for x in p['providers']))
        self.assertTrue(all(x['plan']['declared_tier'] is None for x in p['providers']))
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
    def test_save_provider_persists_only_saved_provider_alerts_without_full_portfolio(self):
        self.store.save_provider(self.snapshot())
        self.assertEqual([a['provider_id'] for a in self.persisted_alerts()],['codex'])
        with self.store.connect() as db:db.execute('DELETE FROM alerts')
        other=self.snapshot();other['id']='claude'
        with patch.object(self.store,'portfolio',side_effect=AssertionError('full portfolio on save')):
            self.store.save_provider(other)
        # Only the changed provider is (re)persisted; the untouched codex alert is not.
        self.assertEqual([a['provider_id'] for a in self.persisted_alerts()],['claude'])
        # The full portfolio remains available and accurate on demand.
        self.assertEqual({a['provider_id'] for a in self.store.portfolio()['alerts']},{'codex','claude'})

    def test_saved_provider_alerts_respect_account_failure_and_reset_boundaries(self):
        self.store.save_provider(self.snapshot());first=self.persisted_alerts('codex')
        self.assertEqual(len(first),1);self.assertEqual(first[0]['kind'],'critical_remaining')
        self.store.save_provider(self.snapshot(account_ref='account-b'));second=self.persisted_alerts('codex')
        self.assertEqual(len(second),2);self.assertNotEqual(first[0]['id'],second[1]['id'] if second[0]['id']==first[0]['id'] else second[0]['id'])
        self.assertEqual(self.store.portfolio()['alerts'][0]['id'],(second[1] if second[0]['id']==first[0]['id'] else second[0])['id'])
        count=len(self.persisted_alerts())
        self.store.save_provider({'id':'codex','status':'auth_required','observed_at':iso(),
            'auth_verified':False,'auth_method':'oauth','source':'test','windows':[],
            'error':{'code':'auth_required','message':'Sign in'}})
        self.assertEqual(len(self.persisted_alerts()),count);self.assertFalse(self.store.portfolio()['alerts'])
        expired=self.snapshot();expired['windows'][0]['resets_at']=iso(self.now-timedelta(seconds=1))
        self.store.save_provider(expired)
        self.assertEqual(len(self.persisted_alerts()),count);self.assertFalse(self.store.portfolio()['alerts'])
        renewed=self.snapshot();renewed['windows'][0]['resets_at']=iso(self.now+timedelta(days=2))
        self.store.save_provider(renewed);after=self.persisted_alerts('codex')
        self.assertEqual(len(after),count+1);self.assertNotIn(after[-1]['id'],{a['id'] for a in second})

    def test_saved_provider_velocity_alert_uses_current_account_and_reset_only(self):
        reset=iso(self.now+timedelta(hours=1))
        for minutes,used in ((6,10),(5,18),(4,26),(3,34),(2,42),(1,46)):
            self.store.record_window('codex','account-a',{'id':'weekly','label':'Weekly','status':'fresh',
                'used_pct':used,'remaining_pct':100-used,'window_minutes':10080,'resets_at':reset,
                'observed_at':iso(self.now-timedelta(minutes=minutes))})
        data=self.snapshot();data['windows'][0].update(used_pct=50,remaining_pct=50,resets_at=reset)
        with patch.object(self.store,'portfolio',side_effect=AssertionError('full portfolio on save')):
            self.store.save_provider(data)
        self.assertEqual([(a['provider_id'],a['kind']) for a in self.persisted_alerts()],
                         [('codex','exhausts_before_reset')])
        self.assertEqual(self.codex()['windows'][0]['velocity']['confidence'],'observed')

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

    def test_legacy_numeric_strings_are_coerced_and_out_of_range_withheld(self):
        data=self.snapshot();data['windows'][0].update(remaining_pct='10',used_pct='90')
        self.store.save_provider(data)
        self.assertEqual(self.codex()['remaining_pct'],10.0)
        self.assertIsInstance(self.codex()['windows'][0]['used_pct'],float)
        data['windows'][0]['remaining_pct']='101';self.store.save_provider(data)
        self.assertIsNone(self.codex()['remaining_pct'])
        self.assertEqual(self.codex()['windows'][0]['status'],'unknown')

    def test_codex_legacy_alias_is_read_only_and_bounded(self):
        account='account-a';reset=iso(self.now+timedelta(hours=1))
        def add(window,minutes,minutes_ago,account_ref=account,used=20):
            self.store.record_window('codex',account_ref,{'id':window,'label':'Legacy' if '-' in window else 'Current',
                'status':'fresh','used_pct':used,'remaining_pct':100-used,'window_minutes':minutes,
                'resets_at':reset,'observed_at':iso(self.now-timedelta(minutes=minutes_ago))})
        add('primary-300m',300,30);add('secondary-10080m',10080,25);add('primary',300,5)
        add('primary-300m',10080,20);add('secondary-10080m',300,15);add('codex-spark',300,10)
        add('primary-300m',300,1,account_ref='account-b',used=99)
        with self.store.connect() as db:before=[tuple(row) for row in db.execute('SELECT window_id,account_ref,window_minutes FROM observations ORDER BY window_id,observed_at')]
        history=self.store.history('codex',hours=2,account=account);series={item['window_id']:item for item in history['series']}
        self.assertEqual(len(series['primary']['points']),2);self.assertEqual(len(series['secondary']['points']),1)
        self.assertEqual(len(series['primary-300m']['points']),1);self.assertEqual(len(series['secondary-10080m']['points']),1)
        self.assertEqual(len(series['codex-spark']['points']),1)
        self.store.portfolio()
        with self.store.connect() as db:after=[tuple(row) for row in db.execute('SELECT window_id,account_ref,window_minutes FROM observations ORDER BY window_id,observed_at')]
        self.assertEqual(before,after)
    def test_codex_legacy_alias_supplies_velocity_only_for_matching_duration(self):
        reset=iso(self.now+timedelta(hours=1));base=self.snapshot()
        base['windows']=[{'id':'primary','label':'5-hour','used_pct':30,'remaining_pct':70,'status':'fresh',
            'observed_at':iso(self.now),'resets_at':reset,'window_minutes':300,'source':'test'}]
        self.store.save_provider(base)
        for minutes_ago,used in ((5,20),(4,22),(3,24),(2,26),(1,28)):
            self.store.record_window('codex','account-a',{'id':'primary-300m','label':'5-hour','status':'fresh',
                'used_pct':used,'remaining_pct':100-used,'window_minutes':300,'resets_at':reset,
                'observed_at':iso(self.now-timedelta(minutes=minutes_ago))})
        window=self.codex()['windows'][0]
        self.assertEqual(window['velocity']['confidence'],'observed');self.assertGreaterEqual(window['velocity']['points'],5)
    def test_codex_wrong_duration_legacy_does_not_supply_velocity(self):
        reset=iso(self.now+timedelta(hours=1));base=self.snapshot()
        base['windows']=[{'id':'primary','label':'5-hour','used_pct':30,'remaining_pct':70,'status':'fresh',
            'observed_at':iso(self.now),'resets_at':reset,'window_minutes':300,'source':'test'}]
        self.store.save_provider(base)
        for minutes_ago,used in ((5,20),(4,22),(3,24),(2,26),(1,28)):
            self.store.record_window('codex','account-a',{'id':'primary-300m','label':'5-hour','status':'fresh',
                'used_pct':used,'remaining_pct':100-used,'window_minutes':10080,'resets_at':reset,
                'observed_at':iso(self.now-timedelta(minutes=minutes_ago))})
        self.assertNotEqual(self.codex()['windows'][0]['velocity']['confidence'],'observed')

if __name__=='__main__':unittest.main()
