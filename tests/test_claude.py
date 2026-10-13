import json
import io
import urllib.error
import tempfile
import unittest
import subprocess
from datetime import datetime, timezone, timedelta
from email.utils import format_datetime
from unittest.mock import patch
from aiusage.claude import normalize,collect
from aiusage import claude
from aiusage.store import Store

class ClaudeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.s=Store(self.temp.name)
        self.profile={'account':{'uuid':'test-account','has_claude_max':True,'email':'private@example.com'},
            'organization':{'uuid':'test-org','rate_limit_tier':'default_claude_max_20x'}}
        claude.retry_credentials();claude.forget_profile();claude.clear_rate_limit()
        self.addCleanup(claude.retry_credentials);self.addCleanup(claude.forget_profile);self.addCleanup(claude.clear_rate_limit)
    def test_live_weekly_exhaustion_and_tier(self):
        r=normalize({'five_hour':{'utilization':15},'seven_day':{'utilization':100}},self.profile,self.s)
        self.assertEqual(r['verified_tier'],'Max 20×');self.assertEqual(r['windows'][1]['remaining_pct'],0)
        self.assertNotIn('private@example',json.dumps(r));self.assertNotIn('test-account',json.dumps(r))
    def test_unrecognized_pool_and_billing_not_coding_quota(self):
        r=normalize({'extra_usage':{'utilization':0},'unknown':{'utilization':0}},self.profile,self.s)
        self.assertFalse(r['windows']);self.assertEqual(r['status'],'partial')
    def test_invalid_values_unknown(self):
        for v in (None,True,-1,'NaN'):
            r=normalize({'seven_day':{'utilization':v}},self.profile,self.s)
            self.assertIsNone(r['windows'][0]['remaining_pct'])
    def test_resets_outside_reported_cadence_are_withheld(self):
        r=normalize({'five_hour':{'utilization':12,'resets_at':(datetime.now(timezone.utc)+timedelta(days=10)).isoformat()},
                     'seven_day':{'utilization':63,'resets_at':(datetime.now(timezone.utc)+timedelta(days=30)).isoformat()}},self.profile,self.s)
        self.assertEqual([w['remaining_pct'] for w in r['windows']],[88,37])
        self.assertEqual([w['resets_at'] for w in r['windows']],[None,None])
    def test_identity_required(self):
        with self.assertRaises(ValueError):normalize({}, {},self.s)
    def test_auth_missing(self):
        with patch('aiusage.claude.subprocess.run') as run:
            run.return_value.returncode=1;r=collect(self.s)
        self.assertEqual(r['status'],'auth_required')
    def test_rate_limit_uses_constant_message_and_retry_delay(self):
        e=urllib.error.HTTPError('https://api.anthropic.com',429,'private',{'Retry-After':'0'},io.BytesIO(b'secret'))
        with patch('aiusage.claude.subprocess.run') as run,patch('aiusage.claude.read_json',side_effect=e):
            run.return_value.returncode=0;run.return_value.stdout=json.dumps({'claudeAiOauth':{'accessToken':'private'}}).encode()
            r=collect(self.s)
        self.assertEqual(r['error']['code'],'rate_limited');self.assertEqual(r['retry_after_seconds'],60)
        self.assertNotIn('secret',json.dumps(r))
    def test_scope_org_separates_history(self):
        a=normalize({},self.profile,self.s)['account_ref']
        self.profile['organization']['uuid']='other'
        self.assertNotEqual(a,normalize({},self.profile,self.s)['account_ref'])
    def test_keychain_timeouts_latch_until_explicit_refresh(self):
        with patch('aiusage.claude.subprocess.run',side_effect=subprocess.TimeoutExpired('security',5)) as run:
            self.assertEqual(collect(self.s)['status'],'auth_required')
            self.assertEqual(collect(self.s)['status'],'auth_required')
            self.assertEqual(run.call_count,1)
            claude.retry_credentials();collect(self.s);self.assertEqual(run.call_count,2)
    def test_observation_age_starts_before_network_requests(self):
        claude._profile_cache.clear();claude._profile_cache.update(credential_ref=None,data=None,until=0,observed_at=None)
        stamp='2026-10-12T00:00:00Z'
        with patch('aiusage.claude.credentials',return_value='synthetic'),patch('aiusage.claude.iso',return_value=stamp),\
                patch('aiusage.claude.read_json',side_effect=[{'seven_day':{'utilization':20}},self.profile]):
            result=collect(self.s)
        self.assertEqual(result['observed_at'],stamp)
    def test_profile_cache_contains_no_raw_token_and_reuses_profile(self):
        claude._profile_cache.clear();claude._profile_cache.update(credential_ref=None,data=None,until=0,observed_at=None)
        with patch('aiusage.claude.credentials',return_value='sentinel-access-token'),patch('aiusage.claude.read_json') as read:
            read.side_effect=[{'seven_day':{'utilization':20}},self.profile,{'seven_day':{'utilization':21}}]
            collect(self.s);collect(self.s)
        self.assertEqual(read.call_count,3)
        self.assertNotIn('sentinel-access-token',json.dumps(claude._profile_cache))

    def test_auth_failure_discards_cached_profile_association(self):
        claude.forget_profile()
        with patch('aiusage.claude.credentials',return_value='synthetic'),patch('aiusage.claude.read_json') as read:
            read.side_effect=[{'seven_day':{'utilization':20}},self.profile,
                urllib.error.HTTPError('https://api.anthropic.com',401,'private',{},None),
                {'seven_day':{'utilization':21}},self.profile]
            self.assertTrue(collect(self.s)['auth_verified'])
            self.assertEqual(collect(self.s)['status'],'auth_required')
            self.assertIsNone(claude._profile_cache['credential_ref'])
            self.assertIsNone(claude._profile_cache['data'])
            self.assertTrue(collect(self.s)['auth_verified'])
            self.assertEqual(read.call_count,5)

    def test_retry_after_parses_seconds_and_http_date_and_rejects_garbage(self):
        now=datetime(2026,10,13,12,0,tzinfo=timezone.utc)
        self.assertEqual(claude.parse_retry_after({'Retry-After':'120'},now=now),120)
        self.assertEqual(claude.parse_retry_after({'Retry-After':format_datetime(now+timedelta(seconds=90))},now=now),90)
        self.assertEqual(claude.parse_retry_after({'Retry-After':format_datetime(now-timedelta(seconds=5))},now=now),0)
        for headers in (None,{},{'Retry-After':'nan'},{'Retry-After':'not-a-date'},{'Retry-After':''}):
            self.assertIsNone(claude.parse_retry_after(headers,now=now))
        self.assertEqual(claude.bounded_retry_delay({'Retry-After':'0'}),60)
        self.assertEqual(claude.bounded_retry_delay({'Retry-After':'999999'}),3600)
        self.assertEqual(claude.bounded_retry_delay({'Retry-After':'garbage'}),60)

    def test_model_weekly_windows_remain_distinct(self):
        r=normalize({'five_hour':{'utilization':10},'seven_day':{'utilization':20},'seven_day_sonnet':{'utilization':30},'seven_day_opus':{'utilization':40}},self.profile,self.s)
        by_id={w['id']:w for w in r['windows']}
        self.assertEqual(by_id['five_hour']['remaining_pct'],90)
        self.assertEqual(by_id['seven_day']['remaining_pct'],80)
        self.assertEqual(by_id['seven_day_sonnet']['remaining_pct'],70)
        self.assertEqual(by_id['seven_day_sonnet']['label'],'Sonnet weekly')
        self.assertEqual(by_id['seven_day_opus']['remaining_pct'],60)

    def _prime_profile_cache(self, observed='2026-10-12T00:00:00Z'):
        with patch('aiusage.claude.credentials',return_value='synthetic'),patch('aiusage.claude.read_json') as read:
            read.side_effect=[{'seven_day':{'utilization':20}},self.profile]
            self.assertTrue(collect(self.s)['auth_verified'])
        # Expire freshness but keep the verified profile usable as stale.
        claude._profile_cache['until']=0
        claude._profile_cache['stale_until']=claude.time.monotonic()+3600
        claude._profile_cache['observed_at']=observed

    def test_profile_429_reuses_only_same_credential_verified_profile(self):
        self._prime_profile_cache()
        error=urllib.error.HTTPError('https://api.anthropic.com',429,'private',{'Retry-After':'120'},io.BytesIO(b'secret'))
        with patch('aiusage.claude.credentials',return_value='synthetic'),patch('aiusage.claude.read_json') as read:
            read.side_effect=[{'five_hour':{'utilization':25},'seven_day':{'utilization':50}},error]
            r=collect(self.s)
        self.assertTrue(r['auth_verified']);self.assertEqual(r['status'],'verified')
        self.assertEqual(r['tier_verified_at'],'2026-10-12T00:00:00Z')
        self.assertEqual({w['id']:w['remaining_pct'] for w in r['windows']},{'five_hour':75,'seven_day':50})
        self.assertNotIn('secret',json.dumps(r))

    def test_profile_429_without_verified_cache_withholds_usage(self):
        claude.forget_profile()
        error=urllib.error.HTTPError('https://api.anthropic.com',429,'private',{'Retry-After':'120'},io.BytesIO(b'secret'))
        with patch('aiusage.claude.credentials',return_value='synthetic'),patch('aiusage.claude.read_json') as read:
            read.side_effect=[{'seven_day':{'utilization':50}},error]
            r=collect(self.s)
        self.assertFalse(r['auth_verified']);self.assertEqual(r['windows'],[])
        self.assertEqual(r['error']['code'],'rate_limited');self.assertEqual(r['retry_after_seconds'],120)

    def test_profile_retry_delay_keeps_valid_usage_without_repolling_profile(self):
        self._prime_profile_cache()
        error=urllib.error.HTTPError('https://api.anthropic.com',429,'private',{'Retry-After':'120'},None)
        with patch('aiusage.claude.credentials',return_value='synthetic'),patch('aiusage.claude.read_json') as read:
            read.side_effect=[{'seven_day':{'utilization':40}},error,{'seven_day':{'utilization':45}}]
            first=collect(self.s);second=collect(self.s)
        self.assertEqual([call.args[0] for call in read.call_args_list],['usage','profile','usage'])
        self.assertEqual(second['windows'][0]['remaining_pct'],55)
        self.assertEqual(first['tier_verified_at'],second['tier_verified_at'])

    def test_profile_429_does_not_reuse_different_credential_profile(self):
        self._prime_profile_cache()
        error=urllib.error.HTTPError('https://api.anthropic.com',429,'private',{'Retry-After':'120'},None)
        with patch('aiusage.claude.credentials',return_value='different-token'),patch('aiusage.claude.read_json') as read:
            read.side_effect=[{'seven_day':{'utilization':50}},error]
            r=collect(self.s)
        self.assertFalse(r['auth_verified']);self.assertEqual(r['windows'],[])

    def test_usage_429_sets_in_process_gate_and_skips_network(self):
        error=urllib.error.HTTPError('https://api.anthropic.com',429,'private',{'Retry-After':'120'},None)
        with patch('aiusage.claude.credentials',return_value='synthetic'),patch('aiusage.claude.read_json',side_effect=error) as read:
            first=collect(self.s);second=collect(self.s)
        self.assertEqual(read.call_count,1)
        self.assertEqual(first['retry_after_seconds'],120)
        self.assertEqual(second['error']['code'],'rate_limited')
        self.assertGreater(second['retry_after_seconds'],0);self.assertLessEqual(second['retry_after_seconds'],120)

    def test_usage_429_accepts_http_date_retry_after(self):
        future=format_datetime(datetime.now(timezone.utc)+timedelta(seconds=180))
        error=urllib.error.HTTPError('https://api.anthropic.com',429,'private',{'Retry-After':future},None)
        with patch('aiusage.claude.credentials',return_value='synthetic'),patch('aiusage.claude.read_json',side_effect=error):
            r=collect(self.s)
        self.assertGreater(r['retry_after_seconds'],60);self.assertLessEqual(r['retry_after_seconds'],180)

if __name__=='__main__':unittest.main()
