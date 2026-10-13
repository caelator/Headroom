import copy
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from aiusage import antigravity as ag
from aiusage.analytics import iso, utc_now
from datetime import timedelta
from aiusage.store import Store


def status(email='demo@example.invalid', fraction=.4):
    return {'userStatus': {'email':email, 'userTier':{'name':'Example tier'},
        'cascadeModelConfigData':{'clientModelConfigs':[{'label':'Gemini example',
            'modelOrAlias':{'model':'gemini-example'}, 'quotaInfo':{
                'remainingFraction':fraction,'resetTime':iso(utc_now()+timedelta(hours=3))}}]},
        'accessToken':'synthetic-do-not-store'}}


def summary(fraction=.25):
    return {'response':{'groups':[{'displayName':'Gemini', 'buckets':[
        {'bucketId':'gemini-session','window':'SESSION','remaining':{
            'case':'remainingFraction','value':fraction},'resetTime':iso(utc_now()+timedelta(hours=2))},
        {'bucketId':'gemini-weekly','window':'weekly','remainingFraction':.6}]}]}}


class AntigravityTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.store=Store(tmp.name)
        self.endpoint=ag.Endpoint(123,12345,'https','synthetic-token')

    def test_quota_summary_explicit_windows_and_identity(self):
        result=ag.normalize(status(),self.store,summary())
        self.assertEqual([w['id'] for w in result['windows']],
            ['antigravity-quota-summary-gemini-session','antigravity-quota-summary-gemini-weekly'])
        self.assertEqual([w['window_minutes'] for w in result['windows']],[300,10080])
        self.assertEqual(result['windows'][0]['remaining_pct'],25)
        self.assertEqual(result['account_ref'],self.store.fingerprint('antigravity|demo@example.invalid'))
        self.assertEqual(result['verified_tier'],'Example tier')
        self.assertNotIn('demo@example.invalid',json.dumps(result))
        self.assertNotIn('synthetic-do-not-store',json.dumps(result))

    def test_model_specific_denominator_and_unknown_cadence(self):
        result=ag.normalize(status(),self.store)
        self.assertEqual(result['windows'][0]['id'],'model-gemini-example')
        self.assertEqual(result['windows'][0]['remaining_pct'],40)
        self.assertIsNone(result['windows'][0]['window_minutes'])

    def test_invalid_fraction_and_disabled_remain_unknown(self):
        for fraction in [True,-.1,1.1,float('nan'),None,'not-quota']:
            with self.subTest(fraction=fraction):
                window=ag.normalize(status(),self.store,summary(fraction))['windows'][0]
                self.assertIsNone(window['remaining_pct']);self.assertEqual(window['status'],'unknown')
        s=summary();s['response']['groups'][0]['buckets'][0]['disabled']=True
        self.assertIsNone(ag.normalize(status(),self.store,s)['windows'][0]['remaining_pct'])

    def test_missing_identity_and_upstream_failure_never_verify(self):
        for payload in [{'userStatus':{}},{'code':True,'userStatus':{'email':'demo@example.invalid'}},
                        {'code':'error','userStatus':{'email':'demo@example.invalid'}}]:
            with self.assertRaises(ValueError):ag.normalize(payload,self.store)

    def test_all_full_models_without_resets_are_availability(self):
        payload=status(fraction=1);payload['userStatus']['cascadeModelConfigData']['clientModelConfigs'][0]['quotaInfo'].pop('resetTime')
        result=ag.normalize(payload,self.store)
        self.assertIsNone(result['windows'][0]['remaining_pct'])

    def test_account_changes_separate_history(self):
        first=ag.normalize(status(),self.store,summary())
        second=ag.normalize(status('other@example.invalid'),self.store,summary())
        self.assertNotEqual(first['account_ref'],second['account_ref'])

    def test_replies_from_different_accounts_are_withheld(self):
        with patch.object(ag,'discover',return_value=[self.endpoint]), patch.object(ag,'local_json',
                side_effect=[status(),summary(),status('other@example.invalid')]):
            result=ag.collect(self.store)
        self.assertEqual(result['status'],'error');self.assertEqual(result['windows'],[])

    def test_distinct_running_accounts_are_ambiguous(self):
        other=ag.Endpoint(124,12346,'https','other-synthetic')
        with patch.object(ag,'discover',return_value=[self.endpoint,other]), patch.object(ag,'local_json',
                side_effect=[status(),summary(),status(),status('other@example.invalid'),summary(),status('other@example.invalid')]):
            result=ag.collect(self.store)
        self.assertEqual(result['error']['code'],'ambiguous_accounts')

    def test_missing_app_never_invokes_external_collector(self):
        with patch.object(ag,'discover',return_value=[]),patch.object(ag,'local_json') as request:
            result=ag.collect(self.store)
        request.assert_not_called();self.assertEqual(result['error']['code'],'app_not_running')

    def test_discovery_requires_own_official_app_and_owned_socket(self):
        app='/Applications/Antigravity.app/Contents/Resources/app/extensions/antigravity/bin/language_server_macos_arm'
        uid=os.getuid();listing=f'{uid} 123 {app}\n{uid+1} 124 {app}\n{uid} 125 /tmp/language_server\n'
        command=app+' --csrf_token synthetic --extension_server_port 12345 --extension_server_csrf_token extension'
        with patch.object(ag,'_process',side_effect=[listing,command,'p123\nn127.0.0.1:12345\nn127.0.0.1:99999\n']):
            endpoints=ag.discover()
        self.assertEqual([(e.port,e.scheme) for e in endpoints],[(12345,'https'),(12345,'http')])
        self.assertNotIn('synthetic',repr(endpoints[0]))
        with patch.object(ag,'_process',side_effect=[listing,command,'p999\nn127.0.0.1:12345\n']):
            self.assertEqual(ag.discover(),[])

    def test_transport_pins_loopback_and_never_redirects_or_proxies(self):
        response=MagicMock();response.__enter__.return_value=io.BytesIO(b'{}')
        opener=MagicMock();opener.open.return_value=response
        with patch.object(ag.urllib.request,'build_opener',return_value=opener) as factory:
            self.assertEqual(ag.local_json(self.endpoint,'GetUserStatus',{},3),{})
        request=opener.open.call_args.args[0]
        self.assertEqual(request.full_url,'https://127.0.0.1:12345'+ag.SERVICE+'GetUserStatus')
        self.assertEqual(factory.call_args.args[0].proxies,{})
        self.assertIsInstance(factory.call_args.args[1],ag.NoRedirect)
        with self.assertRaises(ValueError):ag.local_json(self.endpoint,'SendPrompt',{},3)

    def test_response_limit_and_sanitized_errors(self):
        response=MagicMock();response.__enter__.return_value=io.BytesIO(b'x'*(ag.LIMIT+1))
        opener=MagicMock();opener.open.return_value=response
        with patch.object(ag.urllib.request,'build_opener',return_value=opener):
            with self.assertRaises(ValueError):ag.local_json(self.endpoint,'GetUserStatus',{},3)
        with patch.object(ag,'discover',side_effect=OSError('synthetic-secret')):
            result=ag.collect(self.store)
        self.assertNotIn('synthetic-secret',json.dumps(result))

    def test_same_bucket_id_in_two_groups_is_namespaced_by_group_stably(self):
        def grouped(order):
            groups=[
                {'displayName':'Gemini','buckets':[
                    {'bucketId':'session','window':'SESSION','remainingFraction':.25},
                    {'bucketId':'gemini-weekly','window':'weekly','remainingFraction':.6}]},
                {'displayName':'Claude/GPT','buckets':[
                    {'bucketId':'session','window':'SESSION','remainingFraction':.6}]}]
            return {'response':{'groups':[groups[i] for i in order]}}
        forward=ag.normalize(status(),self.store,grouped([0,1]))
        mapping={w['id']:w['remaining_pct'] for w in forward['windows']}
        self.assertEqual(mapping['antigravity-quota-summary-gemini-session'],25)
        self.assertEqual(mapping['antigravity-quota-summary-claude-gpt-session'],60)
        # The non-colliding bucket keeps its long-standing summary ID.
        self.assertEqual(mapping['antigravity-quota-summary-gemini-weekly'],60)
        reversed_result=ag.normalize(status(),self.store,grouped([1,0]))
        self.assertEqual(mapping,{w['id']:w['remaining_pct'] for w in reversed_result['windows']})
        self.assertEqual(forward['status'],'verified')
    def test_ambiguous_colliding_group_identity_is_partial_not_guessed(self):
        ambiguous={'response':{'groups':[
            {'buckets':[{'bucketId':'session','window':'SESSION','remainingFraction':.25}]},
            {'buckets':[{'bucketId':'session','window':'SESSION','remainingFraction':.6}]}]}}
        result=ag.normalize(status(),self.store,ambiguous)
        self.assertEqual(result['status'],'partial')
        self.assertNotIn('antigravity-quota-summary-session',[w['id'] for w in result['windows']])
    def test_numeric_reset_seconds_are_supported_and_implausible_values_withheld(self):
        s=summary();s['response']['groups'][0]['buckets'][0]['resetTime']=int((utc_now()+timedelta(hours=2)).timestamp())
        window=ag.normalize(status(),self.store,s)['windows'][0]
        self.assertIsNotNone(window['resets_at'])
        for bad in [True,0,10**15,(utc_now()+timedelta(days=4000)).timestamp()]:
            s=summary();s['response']['groups'][0]['buckets'][0]['resetTime']=bad
            self.assertIsNone(ag.normalize(status(),self.store,s)['windows'][0]['resets_at'])

    def test_generic_bucket_ids_do_not_depend_on_payload_membership(self):
        def run(groups):
            return ag.normalize(status(),self.store,{'response':{'groups':groups}})
        gemini={'displayName':'Gemini','buckets':[{'bucketId':'session','window':'SESSION','remainingFraction':.25}]}
        claude={'displayName':'Claude/GPT','buckets':[{'bucketId':'session','window':'SESSION','remainingFraction':.6},
                                                      {'bucketId':'weekly','window':'weekly','remainingFraction':.5}]}
        def mapping(result):return {w['id']:w['remaining_pct'] for w in result['windows']}
        gemini_id='antigravity-quota-summary-gemini-session';claude_id='antigravity-quota-summary-claude-gpt-session'
        # Alone, with another group, reversed, and alone again: IDs never move.
        self.assertEqual(mapping(run([gemini])),{gemini_id:25})
        self.assertEqual(mapping(run([claude]))[claude_id],60)
        self.assertEqual(mapping(run([claude]))['antigravity-quota-summary-claude-gpt-weekly'],50)
        combined=mapping(run([gemini,claude]));self.assertEqual(combined[gemini_id],25);self.assertEqual(combined[claude_id],60)
        self.assertEqual(combined,mapping(run([claude,gemini])))
        self.assertEqual(mapping(run([gemini])),{gemini_id:25})
    def test_provider_qualified_keys_preserved_only_when_consistent_with_group(self):
        def run(group_name,bucket_id):
            s={'response':{'groups':[{'displayName':group_name,'buckets':[
                {'bucketId':bucket_id,'window':'SESSION','remainingFraction':.25}]}]}}
            return ag.normalize(status(),self.store,s)['windows'][0]['id']
        self.assertEqual(run('Gemini','gemini-session'),'antigravity-quota-summary-gemini-session')
        self.assertEqual(run('Claude/GPT','3p-session'),'antigravity-quota-summary-3p-session')
        # A gemini-* key reported under another group is not rebound to the
        # existing gemini series; it is qualified by its reported group.
        self.assertEqual(run('Claude/GPT','gemini-session'),'antigravity-quota-summary-claude-gpt-gemini-session')
        self.assertEqual(run('Gemini','3p-session'),'antigravity-quota-summary-gemini-3p-session')
    def test_generic_bucket_without_group_identity_is_partial_not_guessed(self):
        s={'response':{'groups':[{'buckets':[{'bucketId':'session','window':'SESSION','remainingFraction':.25}]}]}}
        result=ag.normalize(status(),self.store,s)
        self.assertEqual(result['status'],'partial')
        self.assertNotIn('antigravity-quota-summary-session',[w['id'] for w in result['windows']])


if __name__=='__main__':unittest.main()
