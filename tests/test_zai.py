import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from datetime import timedelta
import urllib.error
from unittest.mock import patch
from aiusage import zai
from aiusage.store import Store
from aiusage.analytics import utc_now


def payload():
    return {'success':True,'code':200,'data':{'planName':'Example plan','limits':[
        {'type':'TOKENS_LIMIT','unit':3,'number':5,'percentage':40,'usage':100,'currentValue':40,
         'remaining':60,'nextResetTime':int((utc_now().timestamp()+3600)*1000)},
        {'type':'CREDIT_LIMIT','unit':6,'number':1,'percentage':25}]}}


class ZaiTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        self.store=Store(self.root/'data')
    def test_explicit_percentage_matching_counter_units_and_windows(self):
        result=zai.normalize(payload(),self.store,'synthetic-key','global')
        self.assertEqual([w['id'] for w in result['windows']],['quota-tokens_limit-300','quota-credit_limit-10080'])
        self.assertEqual(result['windows'][0]['remaining_pct'],60)
        self.assertEqual(result['account_ref'],self.store.fingerprint('zai|configured|global|synthetic-key'))
        self.assertNotIn('synthetic-key',json.dumps(result))
    def test_unknown_values_are_not_zero_and_wallet_is_not_quota(self):
        for v in [True,None,float('nan'),-1]:
            p=payload();p['data']['limits'][0]={'type':'TOKENS_LIMIT','unit':3,'number':5,'percentage':v}
            self.assertIsNone(zai.normalize(p,self.store,'k','global')['windows'][0]['remaining_pct'])
        with self.assertRaises(ValueError):zai.normalize({'success':True,'code':200,'data':{'balance':12}},self.store,'k','global')
    def test_explicit_zero_overage_and_unknown_denominator(self):
        for used,remaining in [(0,100),(120,0)]:
            p=payload();p['data']['limits'][0]={'type':'TOKENS_LIMIT','unit':3,'number':5,'percentage':used}
            self.assertEqual(zai.normalize(p,self.store,'k','global')['windows'][0]['remaining_pct'],remaining)
        p=payload();p['data']['limits'][0]={'type':'TOKENS_LIMIT','unit':3,'number':5,'currentValue':4}
        self.assertIsNone(zai.normalize(p,self.store,'k','global')['windows'][0]['remaining_pct'])
    def test_region_and_key_changes_break_identity(self):
        refs={zai.normalize(payload(),self.store,k,r)['account_ref'] for k,r in [('a','global'),('b','global'),('a','bigmodel-cn')]}
        self.assertEqual(len(refs),3)
    def test_monthly_mcp_marker_does_not_invent_one_minute_allowance(self):
        p=payload();p['data']['limits']=[{'type':'TIME_LIMIT','unit':5,'number':1,'percentage':10}]
        w=zai.normalize(p,self.store,'k','global')['windows'][0]
        self.assertEqual(w['id'],'zai-mcp');self.assertIsNone(w['window_minutes'])
    def test_implausible_reset_is_withheld(self):
        p=payload();p['data']['limits'][0]['nextResetTime']=int((utc_now().timestamp()+36000)*1000)
        self.assertIsNone(zai.normalize(p,self.store,'k','global')['windows'][0]['resets_at'])
    def test_environment_needs_no_legacy_config(self):
        with patch.dict(os.environ,{'Z_AI_API_KEY':'synthetic-key'},clear=True),patch.object(Path,'home',return_value=self.root):
            self.assertEqual(zai.credentials()[:2],('synthetic-key','global'))
    def test_legacy_key_is_optional_read_only_compatibility(self):
        file=self.root/'.config/codexbar/config.json';file.parent.mkdir(parents=True)
        content=json.dumps({'providers':[{'id':'zai','apiKey':'synthetic-key','region':'global'}]});file.write_text(content)
        with patch.dict(os.environ,{},clear=True),patch.object(Path,'home',return_value=self.root):
            self.assertEqual(zai.credentials()[:2],('synthetic-key','global'))
        self.assertEqual(file.read_text(),content)
    def test_official_coding_config_and_region_pinning(self):
        file=self.root/'.claude/settings.json';file.parent.mkdir(parents=True)
        file.write_text(json.dumps({'env':{'ANTHROPIC_BASE_URL':'https://api.z.ai/api/anthropic','ANTHROPIC_AUTH_TOKEN':'synthetic-key'}}))
        with patch.dict(os.environ,{},clear=True),patch.object(Path,'home',return_value=self.root):
            self.assertEqual(zai.credentials()[:2],('synthetic-key','global'))
        with patch.dict(os.environ,{'Z_AI_API_KEY':'synthetic-key','Z_AI_QUOTA_URL':'https://evil.invalid'},clear=True),patch.object(Path,'home',return_value=self.root):
            with self.assertRaises(ValueError):zai.credentials()
    def test_collect_calls_fixed_endpoint_and_discards_extras(self):
        p=payload();p['data']['email']='private@example.invalid';p['accessToken']='never-store'
        with patch.object(zai,'credentials',return_value=('synthetic-key','global',None,None)),patch.object(zai,'request_json',return_value=p) as request:
            result=zai.collect(self.store)
        self.assertEqual(request.call_args.args[0],'https://api.z.ai'+zai.QUOTA_PATH)
        self.assertNotIn('private@example.invalid',json.dumps(result));self.assertNotIn('never-store',json.dumps(result))
    def test_auth_and_rate_limit_errors_never_echo_reply(self):
        for code,expected in [(401,'auth_required'),(429,'rate_limited'),(500,'provider_error')]:
            error=urllib.error.HTTPError('https://api.z.ai',code,'synthetic-secret',{'Retry-After':'nan'},None)
            with patch.object(zai,'credentials',return_value=('k','global',None,None)),patch.object(zai,'request_json',side_effect=error):
                result=zai.collect(self.store)
            self.assertEqual(result['error']['code'],expected);self.assertNotIn('synthetic-secret',json.dumps(result))
    def test_no_credentials_has_actionable_auth_status(self):
        with patch.object(zai,'credentials',side_effect=PermissionError):
            self.assertEqual(zai.collect(self.store)['status'],'auth_required')
    def test_reported_percentage_wins_and_conflicting_derived_counters_are_unknown(self):
        p=payload();raw=p['data']['limits'][0];raw.update(percentage=40,usage=100,currentValue=10,remaining=10)
        self.assertEqual(zai.normalize(p,self.store,'k','global')['windows'][0]['used_pct'],40)
        raw.pop('percentage');result=zai.normalize(p,self.store,'k','global')
        self.assertIsNone(result['windows'][0]['used_pct']);self.assertEqual(result['status'],'partial')
        raw.update(currentValue=40,remaining=60)
        self.assertEqual(zai.normalize(p,self.store,'k','global')['windows'][0]['used_pct'],40)
    def test_weekly_far_future_and_seconds_epoch_reset_are_withheld(self):
        p=payload();p['data']['limits'][1]['nextResetTime']=int((utc_now().timestamp()+30*86400)*1000)
        self.assertIsNone(zai.normalize(p,self.store,'k','global')['windows'][1]['resets_at'])
        p['data']['limits'][0]['nextResetTime']=int(utc_now().timestamp()+3600)
        self.assertIsNone(zai.normalize(p,self.store,'k','global')['windows'][0]['resets_at'])

    def test_token_and_credit_same_five_hour_cadence_keep_distinct_denominators(self):
        def run(limits):
            p=payload();p['data']['limits']=limits
            result=zai.normalize(p,self.store,'k','global')
            return result,{w['id']:(w['remaining_pct'],w['label'],w['window_minutes']) for w in result['windows']}
        tokens={'type':'TOKENS_LIMIT','unit':3,'number':5,'percentage':40}
        credit={'type':'CREDIT_LIMIT','unit':3,'number':5,'percentage':25}
        forward,mapping=run([tokens,credit]);_,reversed_mapping=run([credit,tokens])
        self.assertEqual(mapping,reversed_mapping)
        self.assertEqual(mapping['quota-tokens_limit-300'],(60,'5-hour',300))
        self.assertEqual(mapping['quota-credit_limit-300'][0],75)
        self.assertNotEqual(mapping['quota-credit_limit-300'][1],'5-hour')
        self.assertEqual(forward['status'],'verified')
        weekly,mapping=run([{'type':'TOKENS_LIMIT','unit':6,'number':1,'percentage':40},
                             {'type':'CREDIT_LIMIT','unit':6,'number':1,'percentage':25}])
        self.assertEqual(mapping['quota-tokens_limit-10080'][0],60);self.assertEqual(mapping['quota-credit_limit-10080'][0],75)
    def test_time_limit_multiple_cadences_keep_marker_and_qualify_other(self):
        def run(limits):
            p=payload();p['data']['limits']=limits
            return {w['id']:(w['remaining_pct'],w['window_minutes']) for w in zai.normalize(p,self.store,'k','global')['windows']}
        marker={'type':'TIME_LIMIT','unit':5,'number':1,'percentage':10}
        five_hour={'type':'TIME_LIMIT','unit':3,'number':5,'percentage':20}
        self.assertEqual(run([marker,five_hour]),run([five_hour,marker]))
        self.assertEqual(run([marker,five_hour])['zai-mcp'],(90,None))
        self.assertEqual(run([marker,five_hour])['quota-time_limit-300'],(80,300))

    def test_credit_and_time_ids_do_not_depend_on_payload_membership(self):
        def run(limits):
            p=payload();p['data']['limits']=limits
            return {w['id']:(w['remaining_pct'],w['label'],w['window_minutes']) for w in zai.normalize(p,self.store,'k','global')['windows']}
        credit={'type':'CREDIT_LIMIT','unit':3,'number':5,'percentage':25}
        tokens={'type':'TOKENS_LIMIT','unit':3,'number':5,'percentage':40}
        marker_limit={'type':'TIME_LIMIT','unit':5,'number':1,'percentage':10}
        timed={'type':'TIME_LIMIT','unit':3,'number':5,'percentage':20}
        credit_id='quota-credit_limit-300';time_id='quota-time_limit-300'
        # Alone, with another limit, and alone again: the ID never moves.
        self.assertEqual(run([credit])[credit_id],(75,'Credit 5-hour',300))
        self.assertNotIn('primary',run([credit]))
        self.assertEqual(run([credit,tokens])[credit_id],run([credit])[credit_id])
        self.assertEqual(run([tokens])['quota-tokens_limit-300'],run([credit,tokens])['quota-tokens_limit-300'])
        self.assertEqual(run([timed])[time_id],(80,'Time 5-hour',300))
        self.assertNotIn('zai-mcp',run([timed]))
        self.assertEqual(run([timed,marker_limit])[time_id],run([timed])[time_id])
        self.assertEqual(run([marker_limit])['zai-mcp'],run([timed,marker_limit])['zai-mcp'])
        self.assertEqual(run([credit]),run([credit]))

    def test_duplicate_denominator_is_withheld_independent_of_order(self):
        a={'type':'TOKENS_LIMIT','unit':3,'number':5,'percentage':10}
        b={**a,'percentage':80}
        for limits in ([a,b],[b,a]):
            p=payload();p['data']['limits']=limits
            result=zai.normalize(p,self.store,'k','global')
            self.assertEqual(result['status'],'partial');self.assertFalse(result['windows'])

    def test_typed_token_series_cannot_inherit_ambiguous_legacy_credit_history(self):
        result=zai.normalize(payload(),self.store,'k','global')
        window=result['windows'][0]
        for minute in range(1,8):
            self.store.record_window('zai',result['account_ref'],{**window,
                'id':'primary','used_pct':10,'remaining_pct':90,
                'observed_at':(utc_now()-timedelta(minutes=minute)).isoformat()})
        self.store.save_provider(result)
        current=next(p for p in self.store.portfolio()['providers'] if p['id']=='zai')['windows'][0]
        self.assertEqual(current['id'],'quota-tokens_limit-300')
        self.assertLessEqual(current['velocity']['points'],1)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM observations WHERE window_id='primary'").fetchone()[0],7)


if __name__=='__main__':unittest.main()
