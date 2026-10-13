import base64
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
import urllib.error
from unittest.mock import patch
from aiusage import cursor
from aiusage.store import Store


def token(sub='auth0|demo-user',expires=None):
    claims={'sub':sub,'exp':expires if expires is not None else time.time()+3600}
    return 'e30.'+base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')+'.synthetic'


def summary():
    return {'membershipType':'pro','billingCycleStart':'2026-10-01T00:00:00Z','billingCycleEnd':'2026-11-01T00:00:00Z',
            'individualUsage':{'plan':{'used':2000,'limit':10000,'totalPercentUsed':20,
                'autoPercentUsed':.36,'apiPercentUsed':40}}}


class CursorTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name);self.store=Store(self.root/'data')
    def test_explicit_percentages_are_not_fractions(self):
        result=cursor.normalize(summary(),{'sub':'demo-user'},self.store,'demo-user')
        self.assertEqual([w['id'] for w in result['windows']],['primary','secondary','tertiary'])
        self.assertEqual(result['windows'][0]['remaining_pct'],80)
        self.assertAlmostEqual(result['windows'][1]['remaining_pct'],99.64)
        self.assertEqual(result['verified_tier'],'Cursor Pro')
    def test_never_averages_independent_pools(self):
        s=summary();s['individualUsage']['plan']={'autoPercentUsed':20,'apiPercentUsed':80}
        result=cursor.normalize(s,{},self.store,'demo-user')
        self.assertIsNone(result['windows'][0]['remaining_pct'])
        self.assertEqual(result['windows'][1]['remaining_pct'],80)
        self.assertEqual(result['windows'][2]['remaining_pct'],20)
    def test_same_block_cents_ratio_and_invalid_cap(self):
        for used,limit,expected in [(2000,10000,80),(0,10000,100),(12000,10000,0),(2000,0,None),(True,100,None)]:
            s=summary();s['individualUsage']['plan']={'used':used,'limit':limit}
            self.assertEqual(cursor.normalize(s,{},self.store,'demo-user')['windows'][0]['remaining_pct'],expected)
    def test_account_mismatch_and_changes(self):
        with self.assertRaises(ValueError):cursor.normalize(summary(),{'sub':'auth0|other'},self.store,'demo-user')
        first=cursor.normalize(summary(),{},self.store,'demo-user')['account_ref']
        second=cursor.normalize(summary(),{},self.store,'other')['account_ref'];self.assertNotEqual(first,second)
    def test_case_sensitive_accounts_do_not_share_history_scope(self):
        upper=cursor.normalize(summary(),{},self.store,'user_AbC')['account_ref']
        lower=cursor.normalize(summary(),{},self.store,'user_abc')['account_ref']
        self.assertNotEqual(upper,lower)
        self.assertNotEqual(lower,self.store.fingerprint('cursor|user_abc'))
    def test_unknown_tier_drops_personal_fields(self):
        s=summary();s['membershipType']='private@example.invalid';s['accessToken']='never-store'
        result=cursor.normalize(s,{'sub':'demo-user','email':'private@example.invalid'},self.store,'demo-user')
        self.assertIsNone(result['verified_tier']);self.assertNotIn('private@example.invalid',json.dumps(result));self.assertNotIn('never-store',json.dumps(result))
    def test_native_app_database_read_does_not_change_credentials(self):
        path=self.root/'Library/Application Support/Cursor/User/globalStorage/state.vscdb';path.parent.mkdir(parents=True)
        original=token()
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE ItemTable(key TEXT PRIMARY KEY,value TEXT)')
            db.execute('INSERT INTO ItemTable VALUES (?,?)',('cursorAuth/accessToken',original))
        before=path.read_bytes()
        with patch.object(Path,'home',return_value=self.root):
            cookie,subject,legacy=cursor.credentials(self.store)
        self.assertEqual(subject,'demo-user');self.assertIsNone(legacy)
        self.assertTrue(cookie.startswith('WorkosCursorSessionToken=demo-user%3A%3A'))
        self.assertEqual(path.read_bytes(),before)
    def test_real_app_blob_schema_decodes_utf8_credentials(self):
        path=self.root/'Library/Application Support/Cursor/User/globalStorage/state.vscdb';path.parent.mkdir(parents=True)
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE ItemTable(key TEXT PRIMARY KEY,value BLOB)')
            db.execute('INSERT INTO ItemTable VALUES (?,?)',('cursorAuth/accessToken',token().encode()))
        with patch.object(Path,'home',return_value=self.root):
            _,subject,_=cursor.credentials(self.store)
        self.assertEqual(subject,'demo-user')
    def test_invalid_and_expired_jwt_never_authenticate(self):
        for value in ['bad',token(expires=time.time()-1),token(expires=True),token(sub='bad;cookie')]:
            with self.assertRaises(PermissionError):cursor._session(value)
    def test_session_cookie_preserves_case_sensitive_workos_subject(self):
        cookie,subject=cursor._session(token(sub='auth0|user_AbC123'))
        self.assertEqual(subject,'user_AbC123')
        self.assertTrue(cookie.startswith('WorkosCursorSessionToken=user_AbC123%3A%3A'))
    def test_missing_app_does_not_require_codexbar(self):
        with patch.object(Path,'home',return_value=self.root):
            with self.assertRaises(PermissionError):cursor.credentials(self.store)
    def test_existing_session_is_read_without_writing_or_prompts(self):
        cookie='WorkosCursorSessionToken=demo-user%3A%3A'+token()
        raw=json.dumps({'cookieHeader':cookie,'sourceLabel':'synthetic'}).encode()
        path=self.root/'Library/Application Support/CodexBar/cursor-cookie.json';path.parent.mkdir(parents=True);path.write_bytes(raw)
        with patch.object(Path,'home',return_value=self.root):
            value,subject,scope=cursor.credentials(self.store)
        self.assertEqual(subject,'demo-user');self.assertIsNone(scope)
        self.assertEqual(value,cookie)
        self.assertEqual(path.read_bytes(),raw)
    def test_legacy_requests_have_their_own_denominator(self):
        s=summary();s['individualUsage']={}
        result=cursor.normalize(s,{},self.store,'demo-user',requests={'gpt4':{'numRequestsTotal':100,'maxRequestUsage':500}})
        self.assertEqual(result['windows'][-1]['id'],'legacy-requests');self.assertEqual(result['windows'][-1]['remaining_pct'],80)
    def test_fixed_endpoints_and_sanitized_failures(self):
        with patch.object(cursor,'credentials',return_value=('synthetic-cookie','demo-user',None)),patch.object(cursor,'request_json',side_effect=[summary(),{'sub':'demo-user'}]) as request:
            self.assertEqual(cursor.collect(self.store)['status'],'verified')
        self.assertEqual([c.args[0] for c in request.call_args_list],['https://cursor.com/api/usage-summary','https://cursor.com/api/auth/me'])
        for code,expected in [(401,'auth_required'),(429,'rate_limited'),(500,'provider_error')]:
            error=urllib.error.HTTPError('https://cursor.com',code,'private-token',{},None)
            with patch.object(cursor,'credentials',return_value=('cookie','demo-user',None)),patch.object(cursor,'request_json',side_effect=error):
                result=cursor.collect(self.store)
            self.assertEqual(result['error']['code'],expected);self.assertNotIn('private-token',json.dumps(result))
    def test_production_path_requires_confirmed_account(self):
        with patch.object(cursor,'credentials',return_value=('cookie','demo-user',None)),patch.object(cursor,'request_json',side_effect=[summary(),{}]):
            result=cursor.collect(self.store)
        self.assertFalse(result['auth_verified']);self.assertEqual(result['windows'],[])
    def test_reported_fallback_windows_do_not_invent_missing_primary(self):
        for individual,team,requests,key in [({'overall':{'used':20,'limit':100}},None,None,'individual-overall'),
                ({},{'pooled':{'used':20,'limit':100}},None,'team-pooled'),
                ({},None,{'gpt4':{'numRequests':20,'maxRequestUsage':100}},'legacy-requests')]:
            s=summary();s['individualUsage']=individual
            if team:s['teamUsage']=team
            result=cursor.normalize(s,{'sub':'demo-user'},self.store,'demo-user',requests=requests)
            self.assertEqual([w['id'] for w in result['windows']],[key]);self.assertEqual(result['status'],'verified')
    def test_configured_token_rotation_preserves_stable_scope(self):
        file=self.root/'.config/codexbar/config.json';file.parent.mkdir(parents=True)
        def reference(value,org='org'):
            file.write_text(json.dumps({'providers':[{'id':'cursor','tokenAccounts':{'activeIndex':0,'accounts':[
                {'id':'stable','token':'demo-user%3A%3A'+value,'organizationId':org}]}}]}))
            with patch.object(Path,'home',return_value=self.root),patch.object(cursor,'_read_app_token',side_effect=PermissionError):
                return cursor.credentials(self.store)[2]
        first=reference(token());second=reference(token(expires=time.time()+7200))
        self.assertEqual(first,second);self.assertNotEqual(first,reference(token(),'other'))


if __name__=='__main__':unittest.main()
