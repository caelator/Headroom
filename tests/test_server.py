"""Real loopback HTTP boundaries with isolated data and collectors disabled."""
import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import timedelta
from aiusage.analytics import iso,utc_now

ROOT=Path(__file__).resolve().parents[1]

class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));cls.port=sock.getsockname()[1]
        script='''
import sys
import aiusage.server as server
from aiusage.store import Store
from aiusage.analytics import iso
class Quiet:
    synchronizing=False
    def __init__(self,*args):
        import threading
        self.stop=threading.Event()
    def set_meta(self,*args):pass
    def start(self):pass
    def scan(self):pass
    def request_refresh(self):pass
server.Collectors=Quiet
server.CodexQuotaHistory=Quiet
server.ModelImporter=Quiet
server.ModelStore=Quiet
store=Store(sys.argv[1])
store.save_provider({'id':'codex','status':'partial','windows':[{'id':'test','label':'=FORMULA','observed_at':iso(),'source':'@SOURCE','status':'unknown'}]})
server.serve(store,int(sys.argv[2]))
'''
        env=dict(os.environ,PYTHONPATH=str(ROOT/'src'))
        cls.process=subprocess.Popen([sys.executable,'-c',script,cls.temp.name,str(cls.port)],env=env,
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                c=http.client.HTTPConnection('127.0.0.1',cls.port,timeout=1);c.request('GET','/health');r=c.getresponse();r.read();c.close()
                if r.status==200:break
            except OSError:pass
            if cls.process.poll() is not None:raise RuntimeError('Test server failed')
            time.sleep(.03)
        else:raise RuntimeError('Test server did not start')
    @classmethod
    def tearDownClass(cls):
        cls.process.terminate();cls.process.wait(timeout=10);cls.temp.cleanup()
    def request(self,method,path,body=None,headers=None):
        c=http.client.HTTPConnection('127.0.0.1',self.port,timeout=5)
        c.request(method,path,body,headers or {});r=c.getresponse();data=r.read();status=r.status;hdr=dict(r.getheaders());c.close()
        return status,data,hdr
    def post(self,path,obj,**headers):
        return self.request('POST',path,json.dumps(obj),{'Content-Type':'application/json','X-AIUsage-Action':'local',**headers})
    def test_six_plans_and_security_headers(self):
        code,body,headers=self.request('GET','/api/plans');self.assertEqual(code,200)
        self.assertEqual(len(json.loads(body)['providers']),6)
        self.assertEqual(headers['Cache-Control'],'no-store');self.assertIn("frame-ancestors 'none'",headers['Content-Security-Policy'])
    def test_host_and_origin_rejected(self):
        for headers in ({'Host':'evil.test'},{'Origin':'https://evil.test'},{'Origin':'null'}):
            self.assertEqual(self.request('GET','/api/plans',headers=headers)[0],403)
    def test_write_requires_local_action_and_json(self):
        self.assertEqual(self.request('POST','/api/refresh','{}',{'Content-Type':'application/json'})[0],403)
        self.assertEqual(self.request('POST','/api/refresh','{}',{'X-AIUsage-Action':'local'})[0],415)
        self.assertEqual(self.post('/api/refresh',{})[0],202)
    def test_settings_and_validation(self):
        self.assertEqual(self.post('/api/settings',{'refresh_interval_seconds':30})[0],200)
        self.assertEqual(self.post('/api/settings',{'refresh_interval_seconds':300})[0],400)
        self.assertEqual(self.post('/api/settings',{'unknown':True})[0],400)
        self.assertEqual(self.post('/api/settings',[],Origin='https://evil.test')[0],403)
    def test_bad_inputs(self):
        self.assertEqual(self.request('GET','/api/history?provider=invalid')[0],400)
        self.assertEqual(self.request('GET','/api/history?hours=0')[0],400)
        self.assertEqual(self.post('/api/settings',[])[0],400)
    def test_csv_formula_escaping_and_privacy(self):
        code,body,headers=self.request('GET','/api/export.csv');self.assertEqual(code,200)
        self.assertIn(b"'=FORMULA",body);self.assertIn(b"'@SOURCE",body)
        self.assertNotIn(b'account_ref',body);self.assertIn('attachment',headers['Content-Disposition'])
    def test_retired_extension_routes_are_unavailable(self):
        self.assertEqual(self.request('GET','/api/bridge/challenge')[0],404)
        self.assertEqual(self.post('/api/bridge/muse',{})[0],404)
        self.assertEqual(self.request('GET','/api/plans',headers={'Origin':'chrome-extension://old'})[0],403)

if __name__=='__main__':unittest.main()
