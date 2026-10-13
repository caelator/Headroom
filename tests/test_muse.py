import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import patch
import urllib.error
from aiusage.analytics import iso,utc_now
from aiusage.muse import normalize_subscription,collect,credentials,retry_credentials
from aiusage.store import Store

class MuseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.s=Store(self.temp.name)
        retry_credentials();self.addCleanup(retry_credentials)
    def payload(self,**extra):
        reset=int((utc_now()+timedelta(hours=3)).timestamp())
        return {'user_email':'example@private.test','user_full_name':'Secret Name','api_key':'never-store-key',
            'access_token':'never-store-token','is_subs_active':True,'subs_tier_name':'Muse Code Maximum Usage',
            'subs_tier_id':'maximum','subs_usage':{'tier':'maximum',
                'window':{'used_percent':72,'window_duration_mins':300,'resets_at':reset},
                'weekly':{'used_percent':29,'resets_at':reset+86400}},**extra}
    def test_direct_coding_quota(self):
        r=normalize_subscription(self.payload(),self.s)
        self.assertEqual(r['status'],'verified');self.assertEqual(r['verified_tier'],'Muse Code Maximum Usage')
        self.assertEqual([w['remaining_pct'] for w in r['windows']],[28,71])
        self.assertEqual([w['window_minutes'] for w in r['windows']],[300,10080])
        self.assertEqual(r['auth_method'],'oauth')
    def test_privacy_boundary(self):
        r=normalize_subscription(self.payload(),self.s);self.s.save_provider(r)
        with self.s.connect() as db:stored=db.execute("SELECT state FROM providers WHERE id='muse'").fetchone()[0]
        for v in ('example@private.test','Secret Name','never-store-key','never-store-token'):
            self.assertNotIn(v,json.dumps(r));self.assertNotIn(v,stored)
    def test_zero_and_overage_are_valid(self):
        for used,remain in ((0,100),(100,0),(120,0)):
            d=self.payload();d['subs_usage']['window']['used_percent']=used
            self.assertEqual(normalize_subscription(d,self.s)['windows'][0]['remaining_pct'],remain)
    def test_bad_percent_unknown(self):
        for used in (None,True,-10,'NaN',float('inf')):
            d=self.payload();d['subs_usage']['weekly']['used_percent']=used
            r=normalize_subscription(d,self.s);self.assertEqual(r['status'],'partial')
            self.assertIsNone(r['windows'][1]['remaining_pct'])
    def test_inactive_and_api_billing_do_not_create_quota(self):
        r=normalize_subscription(self.payload(is_subs_active=False),self.s)
        self.assertIsNone(r['verified_tier']);self.assertTrue(all(w['remaining_pct'] is None for w in r['windows']))
        r=normalize_subscription(self.payload(subs_usage={'balance':200}),self.s)
        self.assertFalse(r['windows']);self.assertEqual(r['status'],'partial')
    def test_missing_duration_and_reset(self):
        d=self.payload();d['subs_usage']['window'].pop('window_duration_mins');d['subs_usage']['weekly']['resets_at']='invalid'
        r=normalize_subscription(d,self.s);self.assertIsNone(r['windows'][0]['remaining_pct'])
        self.assertIsNone(r['windows'][1]['resets_at'])
    def test_missing_identity_time_or_malformed(self):
        for d in (None,{},self.payload(user_email=None)):
            with self.assertRaises(ValueError):normalize_subscription(d,self.s)
        with self.assertRaises(ValueError):normalize_subscription(self.payload(),self.s,iso(utc_now()+timedelta(minutes=1)))
    def test_identity_changes_do_not_join_history(self):
        a=normalize_subscription(self.payload(),self.s)['account_ref']
        b=normalize_subscription(self.payload(user_email='other@private.test'),self.s)['account_ref']
        self.assertNotEqual(a,b)
    def test_request_send_time_preserved(self):
        stamp=iso(utc_now()-timedelta(seconds=10))
        self.assertEqual(normalize_subscription(self.payload(),self.s,stamp)['observed_at'],stamp)
    def test_keychain_only_existing_oauth_field(self):
        with patch('aiusage.muse.subprocess.run') as run:
            run.return_value.returncode=0;run.return_value.stdout=json.dumps({'access_token':'token','api_key':'other'}).encode()
            self.assertEqual(credentials(),'token');self.assertEqual(run.call_args.kwargs['timeout'],5)
            self.assertIn('ai.meta.dev.credentials',run.call_args.args[0])
    def test_no_repeated_background_permission_dialogs(self):
        with patch('aiusage.muse.subprocess.run',side_effect=subprocess.TimeoutExpired('secret-free',5)) as run:
            for _ in range(3):
                with self.assertRaises(PermissionError):credentials()
            self.assertEqual(run.call_count,1)
            retry_credentials()
            with self.assertRaises(PermissionError):credentials()
            self.assertEqual(run.call_count,2)
    def test_collect_sanitizes_errors_and_keeps_tier_proof(self):
        (Path(self.temp.name)/'muse-verification.json').write_text(json.dumps({'verified_tier':'Muse Code Maximum Usage','observed_at':iso(),'account_ref':'old'}))
        with patch('aiusage.muse.credentials',side_effect=PermissionError('secret')):
            r=collect(self.s)
        self.assertEqual(r['status'],'auth_required');self.assertNotIn('secret',json.dumps(r))
        self.assertEqual(r['verified_tier'],'Muse Code Maximum Usage');self.assertFalse(r['auth_verified'])
    def test_collect_calls_verified_endpoint_and_discards_key(self):
        with patch('aiusage.muse.credentials',return_value='private-token'),patch('aiusage.muse.fetch_subscription',return_value=self.payload()) as fetch:
            r=collect(self.s);fetch.assert_called_once_with('private-token')
        self.assertEqual(r['status'],'verified');self.assertNotIn('never-store-key',json.dumps(r))
    def test_http_failure_not_live(self):
        e=urllib.error.HTTPError('https://api.meta.ai',401,'private',{},io.BytesIO(b'secret'))
        with patch('aiusage.muse.credentials',return_value='private-token'),patch('aiusage.muse.fetch_subscription',side_effect=e):r=collect(self.s)
        self.assertEqual(r['status'],'auth_required');self.assertFalse(r['windows']);self.assertNotIn('secret',json.dumps(r))

if __name__=='__main__':unittest.main()
