import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import patch
from aiusage import codex
from aiusage.store import Store
from aiusage.analytics import utc_now


def window(used=40,seconds=18000):
    return {'used_percent':used,'limit_window_seconds':seconds,'reset_at':int(utc_now().timestamp()+3600)}


def payload():return {'plan_type':'pro','account_id':'demo-account','rate_limit':{'primary_window':window(),'secondary_window':window(20,604800)}}


class CodexTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name);self.store=Store(self.root/'data')
    def test_canonical_windows_and_account_continuity(self):
        result=codex.normalize(payload(),self.store,'demo-account')
        self.assertEqual([w['id'] for w in result['windows']],['primary','secondary'])
        self.assertEqual([w['remaining_pct'] for w in result['windows']],[60,80])
        self.assertEqual(result['account_ref'],self.store.fingerprint('codex|demo-account'))
        self.assertNotIn('demo-account',json.dumps(result))
    def test_weekly_only_and_swapped_slots_keep_roles(self):
        p=payload();p['rate_limit']={'primary_window':window(20,604800)}
        self.assertEqual(codex.normalize(p,self.store,'demo-account')['windows'][0]['id'],'secondary')
        p['rate_limit']['secondary_window']=window()
        self.assertEqual([w['id'] for w in codex.normalize(p,self.store,'demo-account')['windows']],['secondary','primary'])
    def test_zero_overage_invalid_and_missing_values(self):
        for value,expected in [(0,100),(120,0),(None,None),(True,None),(-1,None),(float('nan'),None)]:
            p=payload();p['rate_limit']['primary_window']=window(value)
            self.assertEqual(codex.normalize(p,self.store,'demo-account')['windows'][0]['remaining_pct'],expected)
    def test_wallet_never_becomes_unlimited_subscription(self):
        result=codex.normalize({'credits':{'balance':'12.5','unlimited':True}},self.store,'demo-account')
        self.assertEqual(result['windows'],[]);self.assertEqual(result['wallet']['unit'],'credits')
    def test_extra_spark_windows_and_malformed_siblings(self):
        p=payload();p['additional_rate_limits']=[None,{'limit_name':'Codex Spark','rate_limit':{
            'primary_window':window(10),'secondary_window':window(30,604800)}}]
        result=codex.normalize(p,self.store,'demo-account')
        self.assertEqual([w['id'] for w in result['windows']][-2:],['codex-spark','codex-spark-weekly'])
        self.assertEqual(result['status'],'partial')
    def test_additional_spark_reversed_slots_keep_duration_roles(self):
        p=payload();p['additional_rate_limits']=[{'limit_name':'Codex Spark','rate_limit':{
            'primary_window':window(30,604800),'secondary_window':window(10,18000)}}]
        result=codex.normalize(p,self.store,'demo-account')
        spark=[w for w in result['windows'] if w['id'].startswith('codex-spark')]
        self.assertEqual([(w['id'],w['label'],w['window_minutes'],w['remaining_pct']) for w in spark],[
            ('codex-spark-weekly','Codex Spark Weekly',10080,70),('codex-spark','Codex Spark 5-hour',300,90)])
    def test_additional_non_spark_reversed_slots_keep_duration_roles(self):
        p=payload();p['additional_rate_limits']=[{'limit_name':'Example Model','rate_limit':{
            'primary_window':window(25,604800),'secondary_window':window(45,18000)}}]
        result=codex.normalize(p,self.store,'demo-account')
        extra=[w for w in result['windows'] if w['id'].startswith('codex-example')]
        self.assertEqual([(w['id'],w['label'],w['window_minutes'],w['remaining_pct']) for w in extra],[
            ('codex-example-model-weekly','Example Model Weekly',10080,75),
            ('codex-example-model','Example Model 5-hour',300,55)])
    def test_additional_unexpected_duration_is_not_named_5_hour_or_weekly(self):
        p=payload();p['additional_rate_limits']=[
            {'limit_name':'Codex Spark','rate_limit':{'primary_window':window(15,3600)}},
            {'limit_name':'Example Model','rate_limit':{'secondary_window':window(35,86400)}}]
        result=codex.normalize(p,self.store,'demo-account')
        extra=result['windows'][2:]
        self.assertEqual([(w['id'],w['label'],w['window_minutes']) for w in extra],[
            ('codex-spark-60m','Codex Spark 60-minute window',60),
            ('codex-example-model-1440m','Example Model 1440-minute window',1440)])
        for w in extra:self.assertNotIn(w['label'],('Codex Spark 5-hour','Codex Spark Weekly'))
    def test_additional_missing_duration_is_neutral_and_slot_distinct(self):
        def undated(used):return {'used_percent':used,'reset_at':int(utc_now().timestamp()+3600)}
        p=payload();p['additional_rate_limits']=[
            {'limit_name':'Codex Spark','rate_limit':{'primary_window':undated(10),'secondary_window':undated(20)}},
            {'limit_name':'Example Model','rate_limit':{'primary_window':undated(30),'secondary_window':undated(40)}}]
        result=codex.normalize(p,self.store,'demo-account')
        extra=result['windows'][2:]
        self.assertEqual([w['id'] for w in extra],['codex-spark-primary-unknown','codex-spark-secondary-unknown',
            'codex-example-model-primary-unknown','codex-example-model-secondary-unknown'])
        self.assertTrue(all(w['window_minutes'] is None for w in extra))
        self.assertTrue(all('Weekly' not in w['label'] and '5-hour' not in w['label'] for w in extra))
        self.assertEqual([w['remaining_pct'] for w in extra],[90,80,70,60])
    def test_global_reversed_slots_attach_values_to_duration_roles(self):
        p=payload();p['rate_limit']={'primary_window':window(22,604800),'secondary_window':window(44,18000)}
        result=codex.normalize(p,self.store,'demo-account')
        self.assertEqual([(w['id'],w['label'],w['window_minutes'],w['remaining_pct']) for w in result['windows']],[
            ('secondary','Weekly',10080,78),('primary','5-hour',300,56)])

    def test_global_unexpected_or_missing_duration_is_neutral(self):
        def undated(used):return {'used_percent':used,'reset_at':int(utc_now().timestamp()+3600)}
        p=payload();p['rate_limit']={'primary_window':window(15,3600),'secondary_window':undated(25)}
        result=codex.normalize(p,self.store,'demo-account')
        self.assertEqual([(w['id'],w['label'],w['window_minutes'],w['remaining_pct']) for w in result['windows']],[
            ('codex-primary_window-60','Current window',60,85),('codex-secondary_window-unknown','Current window',None,75)])

    def test_nonintegral_minutes_do_not_round_into_a_known_cadence(self):
        p=payload();p['rate_limit']={'primary_window':window(22,18001)}
        w=codex.normalize(p,self.store,'demo-account')['windows'][0]
        self.assertIsNone(w['window_minutes']);self.assertEqual(w['label'],'Current window')

    def test_duplicate_cadence_is_withheld_independent_of_slot_order(self):
        for values in ((22,44),(44,22)):
            p=payload();p['rate_limit']={'primary_window':window(values[0],604800),'secondary_window':window(values[1],604800)}
            result=codex.normalize(p,self.store,'demo-account')
            self.assertEqual(result['status'],'partial');self.assertFalse(result['windows'])

    def test_individual_allowance_uses_matching_units(self):
        p=payload();p['individual_limit']={'used':2,'limit':10}
        result=codex.normalize(p,self.store,'demo-account')
        self.assertEqual(result['windows'][-1]['id'],'codex-individual-limit');self.assertEqual(result['windows'][-1]['remaining_pct'],80)
    def test_mismatch_rejected_and_missing_identity_withheld(self):
        with self.assertRaises(ValueError):codex.normalize(payload(),self.store,'other-account')
        p=payload();p.pop('account_id');result=codex.normalize(p,self.store,None)
        self.assertIsNone(result['account_ref']);self.assertEqual(result['status'],'partial')
    def test_credentials_honor_codex_home_and_are_read_only(self):
        file=self.root/'auth.json';content=json.dumps({'tokens':{'access_token':'synthetic-token','account_id':'demo-account'}});file.write_text(content)
        with patch.dict(os.environ,{'CODEX_HOME':str(self.root)},clear=True):
            self.assertEqual(codex.credentials(),('synthetic-token','demo-account'))
        self.assertEqual(file.read_text(),content)
    def test_api_keys_do_not_establish_oauth_quota(self):
        (self.root/'auth.json').write_text(json.dumps({'OPENAI_API_KEY':'synthetic-key'}))
        with patch.dict(os.environ,{'CODEX_HOME':str(self.root)},clear=True):
            with self.assertRaises(PermissionError):codex.credentials()
    def test_collect_fixed_recipient_and_sensitive_field_discard(self):
        p=payload();p['email']='private@example.invalid';p['token']='private-token'
        with patch.object(codex,'credentials',return_value=('synthetic-token','demo-account')),patch.object(codex,'request_json',return_value=p) as request:
            result=codex.collect(self.store)
        self.assertEqual(request.call_args.args[0],codex.ENDPOINT)
        self.assertEqual(request.call_args.kwargs['headers']['ChatGPT-Account-Id'],'demo-account')
        self.assertNotIn('private@example.invalid',json.dumps(result));self.assertNotIn('private-token',json.dumps(result))
    def test_auth_and_rate_limit_errors_are_constant(self):
        for code,expected in [(401,'auth_required'),(429,'rate_limited'),(500,'provider_error')]:
            error=urllib.error.HTTPError(codex.ENDPOINT,code,'private-token',{'Retry-After':'nan'},None)
            with patch.object(codex,'credentials',return_value=('t','demo-account')),patch.object(codex,'request_json',side_effect=error):result=codex.collect(self.store)
            self.assertEqual(result['error']['code'],expected);self.assertNotIn('private-token',json.dumps(result))
    def test_implausible_reset_keeps_percentage_without_projection_boundary(self):
        p=payload();p['rate_limit']['primary_window']['reset_at']=int(utc_now().timestamp()+365*86400)
        window=codex.normalize(p,self.store,'demo-account')['windows'][0]
        self.assertEqual(window['remaining_pct'],60);self.assertIsNone(window['resets_at'])


if __name__=='__main__':unittest.main()
