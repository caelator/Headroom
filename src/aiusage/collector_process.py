"""One persistent, killable quota process per provider with bounded RPC."""
import hashlib
import hmac
import importlib
import json
import multiprocessing
import os
from pathlib import Path
import signal
import struct
import threading
import time
from .analytics import iso

MAX_RESPONSE=1024*1024


def failure(provider,code='collection_timeout'):
    return {'id':provider,'observed_at':iso(),'status':'error','windows':[],
        'source':'collector','auth_method':'unknown','auth_verified':False,'account_ref':None,
        'error':{'code':code,'message':'Provider collection did not complete within its bounded deadline.'},
        'retry_after_seconds':60}


class Context:
    def __init__(self,directory):
        self.root=Path(directory);self.salt=(self.root/'identity-salt').read_bytes()
        if len(self.salt)!=32:raise ValueError('Invalid identity salt')
    def fingerprint(self,value):return hmac.new(self.salt,str(value).encode(),hashlib.sha256).hexdigest()[:32]


def watch_parent():
    """An abruptly stopped service must not leave credential readers running."""
    owner=os.getppid();group=os.getpid()
    def monitor():
        while os.getppid()==owner:time.sleep(.25)
        # worker() created this session/group; all its subprocesses belong to it.
        try:os.killpg(group,signal.SIGKILL)
        except ProcessLookupError:pass
    threading.Thread(target=monitor,daemon=True).start()


def worker(provider,connection,directory):
    os.setsid()
    watch_parent()
    null=os.open(os.devnull,os.O_WRONLY)
    os.dup2(null,1);os.dup2(null,2);os.close(null)
    try:
        context=Context(directory);module=importlib.import_module('aiusage.'+provider)
        connection.send_bytes(b'{"ready":true}')
        while True:
            message=json.loads(connection.recv_bytes(1024))
            if message.get('action')=='stop':return
            if message.get('refresh') and hasattr(module,'retry_credentials'):module.retry_credentials()
            try:result=module.collect(context)
            except Exception:result=failure(provider,'collector_error')
            try:data=json.dumps(result,allow_nan=False).encode()
            except (TypeError,ValueError):data=json.dumps(failure(provider,'invalid_payload')).encode()
            if len(data)>MAX_RESPONSE:data=json.dumps(failure(provider,'response_too_large')).encode()
            connection.send_bytes(data)
    except (OSError,EOFError,ValueError):pass
    finally:connection.close()


class ProviderProcess:
    def __init__(self,provider,directory,deadline=45,target=worker):
        self.provider=provider;self.directory=str(directory);self.deadline=deadline;self.target=target
        self.process=None;self.connection=None;self.ready=False;self.close_lock=threading.Lock()

    def start(self,budget):
        if self.process is not None:
            if self.process.is_alive():return self.ready
            self.close()
        context=multiprocessing.get_context('spawn');parent,child=context.Pipe(duplex=True)
        process=context.Process(target=self.target,args=(self.provider,child,self.directory),daemon=True,
            name='headroom-quota-'+self.provider)
        process.start();child.close();self.process=process;self.connection=parent
        try:self.ready=json.loads(self.receive(parent,time.monotonic()+budget,1024)).get('ready') is True
        except (OSError,EOFError,ValueError,TimeoutError):self.ready=False
        if not self.ready:self.close()
        return self.ready

    @staticmethod
    def receive(connection,until,limit):
        def exact(size):
            result=bytearray()
            while len(result)<size:
                remaining=until-time.monotonic()
                if remaining<=0 or not connection.poll(remaining):raise TimeoutError('RPC deadline')
                chunk=os.read(connection.fileno(),size-len(result))
                if not chunk:raise EOFError('RPC closed')
                result.extend(chunk)
            return bytes(result)
        length=struct.unpack('!i',exact(4))[0]
        if not 0<=length<=limit:raise ValueError('RPC response limit')
        return exact(length)

    def fetch(self,refresh=False):
        until=time.monotonic()+self.deadline
        if not self.start(max(.001,until-time.monotonic())):return failure(self.provider)
        connection=self.connection
        try:
            connection.send_bytes(json.dumps({'action':'fetch','refresh':refresh}).encode())
            result=json.loads(self.receive(connection,until,MAX_RESPONSE))
            if not isinstance(result,dict) or result.get('id')!=self.provider:
                self.close();return failure(self.provider,'invalid_payload')
            return result
        except TimeoutError:self.close();return failure(self.provider)
        except (OSError,EOFError,ValueError,RecursionError):
            self.close();return failure(self.provider,'collector_error')

    def close(self,timeout=2):
        with self.close_lock:
            process=self.process
            if process is not None and process.is_alive():
                try:
                    if os.getpgid(process.pid)==process.pid:os.killpg(process.pid,signal.SIGTERM)
                    else:process.terminate()
                except ProcessLookupError:pass
                process.join(timeout=max(0,min(1,timeout/2)))
                if process.is_alive():
                    try:
                        if os.getpgid(process.pid)==process.pid:os.killpg(process.pid,signal.SIGKILL)
                        else:process.kill()
                    except ProcessLookupError:pass
                    process.join(timeout=max(0,min(1,timeout/2)))
            if self.connection is not None:self.connection.close();self.connection=None
            self.ready=False
            if process is None or not process.is_alive():
                if process is not None:process.close()
                self.process=None
