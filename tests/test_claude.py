import json
import io
import urllib.error
import tempfile
import unittest
import subprocess
from unittest.mock import patch
from aiusage.claude import normalize,collect
from aiusage import claude
from aiusage.store import Store

class ClaudeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.s=Store(self.temp.name)
        self.profile={'account':{'uuid':'test-account','has_claude_max':True,'email':'private@example.com'},
            'organization':{'uuid':'test-org','rate_limit_tier':'default_claude_max_20x'}}
        claude.retry_credentials();self.addCleanup(claude.retry_credentials)
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

if __name__=='__main__':unittest.main()
