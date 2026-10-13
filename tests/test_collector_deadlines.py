import json
import os
from pathlib import Path
import struct
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from aiusage.collector_process import ProviderProcess


def sleeping_worker(provider,connection,directory):
    os.setsid();connection.send_bytes(b'{"ready":true}')
    while True:
        connection.recv_bytes();(Path(directory)/'attempt').write_text('started');time.sleep(5)


def fast_worker(provider,connection,directory):
    os.setsid();connection.send_bytes(b'{"ready":true}')
    while True:
        connection.recv_bytes();connection.send_bytes(json.dumps({'id':provider,'status':'partial','windows':[]}).encode())


def drip_worker(provider,connection,directory):
    os.setsid();connection.send_bytes(b'{"ready":true}')
    connection.recv_bytes();os.write(connection.fileno(),struct.pack('!i',100))
    for _ in range(100):os.write(connection.fileno(),b' ');time.sleep(.05)


class ProcessDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.workers=[]
        self.addCleanup(lambda:[worker.close() for worker in self.workers])
    def make(self,target,provider='test'):
        worker=ProviderProcess(provider,self.tmp.name,deadline=.25,target=target);self.workers.append(worker)
        self.assertTrue(worker.start(5));return worker
    def test_blocked_collection_is_killed_and_other_provider_continues(self):
        slow=self.make(sleeping_worker,'slow');fast=self.make(fast_worker,'fast')
        result=[];started=time.monotonic()
        thread=threading.Thread(target=lambda:result.append(slow.fetch()));thread.start()
        self.assertEqual(fast.fetch()['id'],'fast');thread.join(timeout=2)
        self.assertFalse(thread.is_alive());self.assertLess(time.monotonic()-started,2)
        self.assertEqual(result[0]['error']['code'],'collection_timeout');self.assertIsNone(slow.process)
    def test_rpc_drip_cannot_extend_collection_deadline(self):
        worker=self.make(drip_worker);started=time.monotonic();result=worker.fetch()
        self.assertEqual(result['error']['code'],'collection_timeout');self.assertLess(time.monotonic()-started,2)
        self.assertIsNone(worker.process)
    def test_persistent_worker_reuses_process_and_closes_cleanly(self):
        worker=self.make(fast_worker);pid=worker.process.pid
        self.assertEqual(worker.fetch()['id'],'test');self.assertEqual(worker.fetch()['id'],'test')
        self.assertEqual(worker.process.pid,pid);worker.close();self.assertIsNone(worker.process)

    def test_abrupt_owner_exit_terminates_provider_group(self):
        marker=Path(self.tmp.name)/'child-pid'
        script='''
import os,multiprocessing,sys,time
from pathlib import Path
from aiusage.collector_process import watch_parent
def child():
    os.setsid();watch_parent();Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(60)
p=multiprocessing.get_context('fork').Process(target=child);p.start()
while not Path(sys.argv[1]).exists():time.sleep(.01)
os._exit(0)
'''
        subprocess.run([sys.executable,'-c',script,str(marker)],check=True,timeout=5)
        pid=int(marker.read_text())
        try:
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                try:os.kill(pid,0)
                except ProcessLookupError:break
                time.sleep(.05)
            else:self.fail('Provider group survived abrupt owner exit')
        finally:
            try:os.killpg(pid,signal.SIGKILL)
            except ProcessLookupError:pass


if __name__=='__main__':unittest.main()
