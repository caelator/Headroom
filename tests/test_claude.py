import json
import io
import urllib.error
import tempfile
import unittest
from unittest.mock import patch
from aiusage.claude import normalize,collect
from aiusage.store import Store

class ClaudeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.s=Store(self.temp.name)
        self.profile={'account':{'uuid':'test-account','has_claude_max':True,'email':'private@example.com'},
            'organization':{'uuid':'test-org','rate_limit_tier':'default_claude_max_20x'}}
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

if __name__=='__main__':unittest.main()
