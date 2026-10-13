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
from pathlib import Path
from datetime import timedelta
from aiusage.analytics import utc_now
from aiusage.local_models import Store as Ledger
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
ledger=Ledger(Path(sys.argv[1])/'model-test.sqlite3')
ledger.set_meta('last_scan',iso())
ledger.observe({'key':'past','model':'fixture-model','timestamp':iso(utc_now()-timedelta(seconds=20)), 'total_tokens':10})
ledger.observe({'key':'future','model':'fixture-model','timestamp':iso(utc_now()+timedelta(days=1)), 'total_tokens':1000})
server.ModelStore=lambda:ledger
server.model_catalog=lambda:[]
server.HTTP_TIMEOUT_SECONDS=.4
store=Store(sys.argv[1])
_portfolio=store.portfolio
def counting_portfolio(*args,**kwargs):
    with open(Path(sys.argv[1])/'portfolio-calls','a') as marker:marker.write('x')
    return _portfolio(*args,**kwargs)
store.portfolio=counting_portfolio
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
    def portfolio_calls(self):
        marker=Path(self.temp.name)/'portfolio-calls'
        return len(marker.read_text()) if marker.exists() else 0
    def test_health_is_cheap_identity_and_plans_stays_full(self):
        before=self.portfolio_calls()
        code,body,_=self.request('GET','/health');self.assertEqual(code,200)
        payload=json.loads(body);self.assertEqual(set(payload),{'service','synchronizing'})
        self.assertIs(payload['synchronizing'],False)
        self.assertEqual(payload['service']['name'],'headroom');self.assertEqual(payload['service']['api_version'],1)
        self.assertEqual(payload['service']['pid'],self.process.pid)
        self.assertEqual(len(payload['service']['source_fingerprint']),64)
        self.assertEqual(self.request('GET','/health')[0],200)
        self.assertEqual(self.portfolio_calls(),before)
        code,body,_=self.request('GET','/api/plans');self.assertEqual(code,200)
        self.assertEqual(len(json.loads(body)['providers']),6);self.assertEqual(self.portfolio_calls(),before+1)
    def test_six_plans_and_security_headers(self):
        code,body,headers=self.request('GET','/api/plans');self.assertEqual(code,200)
        self.assertEqual(len(json.loads(body)['providers']),6)
        self.assertEqual(headers['Cache-Control'],'no-store');self.assertIn("frame-ancestors 'none'",headers['Content-Security-Policy'])
        identity=json.loads(body)['service']
        self.assertEqual(identity['name'],'headroom');self.assertEqual(identity['api_version'],1)
        self.assertEqual(identity['pid'],self.process.pid);self.assertEqual(len(identity['source_fingerprint']),64)
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
    def test_incomplete_post_expires_and_does_not_block_other_clients(self):
        with socket.create_connection(('127.0.0.1',self.port),timeout=2) as sock:
            sock.sendall(f'POST /api/settings HTTP/1.0\r\nHost: 127.0.0.1:{self.port}\r\nContent-Type: application/json\r\nX-AIUsage-Action: local\r\nContent-Length: 100\r\n\r\n{{'.encode())
            self.assertEqual(self.request('GET','/health')[0],200)
            response=sock.recv(4096)
            self.assertIn(b'408',response)
    def test_ambiguous_or_chunked_lengths_are_rejected(self):
        for framing in ['Content-Length: 2\r\nContent-Length: 2','Transfer-Encoding: chunked\r\nContent-Length: 2']:
            with socket.create_connection(('127.0.0.1',self.port),timeout=2) as sock:
                sock.sendall(f'POST /api/settings HTTP/1.0\r\nHost: 127.0.0.1:{self.port}\r\nContent-Type: application/json\r\nX-AIUsage-Action: local\r\n{framing}\r\n\r\n{{}}'.encode())
                self.assertIn(b'400',sock.recv(4096))
    def test_trickled_post_cannot_extend_absolute_read_deadline(self):
        with socket.create_connection(('127.0.0.1',self.port),timeout=2) as sock:
            sock.sendall(f'POST /api/settings HTTP/1.0\r\nHost: 127.0.0.1:{self.port}\r\nContent-Type: application/json\r\nX-AIUsage-Action: local\r\nContent-Length: 100\r\n\r\n{{'.encode())
            time.sleep(.12);sock.sendall(b' ');time.sleep(.12);sock.sendall(b' ')
            self.assertIn(b'408',sock.recv(4096))
    def test_bad_query_errors_do_not_reflect_private_values(self):
        code,body,_=self.request('GET','/api/history?hours=private-sentinel')
        self.assertEqual(code,400);self.assertNotIn(b'private-sentinel',body)
    def test_future_receipts_do_not_inflate_present_rates(self):
        code,body,_=self.request('GET','/api/local-usage?days=1')
        self.assertEqual(code,200);report=json.loads(body)
        self.assertEqual(report['totals']['total_tokens'],10)
        self.assertEqual(report['velocity'][0]['total_tokens_per_minute'],10/15)
    def test_port_collision_prevents_provider_collection(self):
        marker=Path(self.temp.name)/'should-not-start'
        script='''
import sys
from pathlib import Path
import aiusage.server as server
from aiusage.store import Store
class Quiet:
    def __init__(self,*args):pass
    def start(self):Path(sys.argv[3]).write_text('started')
server.Collectors=Quiet
try:server.serve(Store(sys.argv[1]),int(sys.argv[2]))
except OSError:raise SystemExit(73)
'''
        p=subprocess.run([sys.executable,'-c',script,self.temp.name,str(self.port),str(marker)],
            env=dict(os.environ,PYTHONPATH=str(ROOT/'src')),capture_output=True,timeout=5)
        self.assertEqual(p.returncode,73);self.assertFalse(marker.exists())

    def test_failed_startup_closes_bound_port_and_collector_owner(self):
        script='''
import sys,socket,threading
import aiusage.server as server
from aiusage.store import Store
closed=[]
class Quiet:
    def __init__(self,*args):self.stop=threading.Event()
    def close(self,timeout=5):closed.append(True)
server.Collectors=Quiet
def broken():raise ValueError('synthetic startup failure')
server.ModelStore=broken
with socket.socket() as probe:
    probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
try:server.serve(Store(sys.argv[1]),port)
except ValueError:pass
else:raise AssertionError('startup must fail')
assert closed==[True]
with socket.socket() as probe:probe.bind(('127.0.0.1',port))
'''
        p=subprocess.run([sys.executable,'-c',script,self.temp.name],
            env=dict(os.environ,PYTHONPATH=str(ROOT/'src')),capture_output=True,timeout=5)
        self.assertEqual(p.returncode,0,p.stderr.decode())

if __name__=='__main__':unittest.main()
