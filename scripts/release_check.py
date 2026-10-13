#!/usr/bin/env python3
"""Repeatable local release gate with a source-bound, sanitized receipt."""
import argparse
from datetime import datetime,timezone,timedelta
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from aiusage.release import build_manifest
from aiusage.registry import PROVIDERS


def validation_fingerprint():
    paths=[ROOT/name for name in ('README.md','AGENTS.md','SECURITY.md','CONTRIBUTING.md')]
    for name in ('tests','docs'):
        paths.extend(path for path in (ROOT/name).rglob('*') if path.is_file() and
                     '__pycache__' not in path.parts and path.suffix in ('.py','.js','.json','.md','.jpg','.svg'))
    digest=hashlib.sha256()
    for path in sorted(paths):
        if path.is_symlink():raise ValueError('Validation inputs must not be symlinks.')
        content=path.read_bytes();name=path.relative_to(ROOT).as_posix()
        digest.update(name.encode()+b'\0'+str(len(content)).encode()+b'\0'+content)
    return digest.hexdigest()


def run(argv):
    result=subprocess.run(argv,cwd=ROOT,capture_output=True,text=True)
    if result.returncode:
        # These are source/build/test outputs only; no live provider probes here.
        print(result.stdout[-12000:]);print(result.stderr[-12000:],file=sys.stderr)
        raise RuntimeError('Local release gate failed: '+Path(argv[0]).name)
    return result


def synthetic_snapshot():
    now=datetime.now(timezone.utc)
    iso=lambda value:value.isoformat().replace('+00:00','Z')
    providers=[]
    for index,(key,spec) in enumerate(PROVIDERS.items()):
        remaining=20+index*10
        window={'id':'synthetic','label':'Example allowance','remaining_pct':remaining,'status':'fresh',
            'observed_at':iso(now),
            'resets_at':iso(now+timedelta(hours=2)),'fresh_until':iso(now+timedelta(seconds=90)),
            'velocity':{'confidence':'insufficient','pct_per_hour':None,'exhausts_before_reset':None}}
        providers.append({'id':key,'name':spec['name'],'status':'verified','remaining_pct':remaining,
            'plan':{'verified_tier':'Example'},'windows':[window],'limiting_window_id':'synthetic'})
    return {'updated_at':iso(now),'providers':providers,'service':{
        'name':'headroom','api_version':1,'source_fingerprint':build_manifest(ROOT)['source_fingerprint'],
        'version':build_manifest(ROOT)['version'],'pid':1}}


def native_gate():
    machine=platform.machine();target=machine+'-apple-macosx13.0'
    with tempfile.TemporaryDirectory(prefix='headroom-native-gate-') as directory:
        binary=Path(directory)/'Headroom'
        run(['xcrun','swiftc','-O','-swift-version','5','-target',target,'-framework','Cocoa',
             str(ROOT/'native/MenuBar.swift'),'-o',str(binary)])
        run([str(binary),'--self-test'])
        run([str(binary),'--widget-self-test'])
        run([str(binary),'--selection-fixture',str(ROOT/'tests/native-selection.json')])
        snapshot=synthetic_snapshot()
        scenario='valid'
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                if scenario=='redirect' and self.path!='/redirected':
                    self.send_response(302);self.send_header('Location','/redirected');self.end_headers();return
                if scenario=='oversized':
                    self.send_response(200);self.send_header('Content-Length',str(1024*1024+1));self.end_headers();return
                if scenario=='streamed_oversized':
                    self.send_response(200);self.end_headers()
                    try:self.wfile.write(b' '* (1024*1024+1))
                    except OSError:pass
                    return
                body=json.dumps(snapshot).encode();self.send_response(200)
                self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)))
                self.end_headers();self.wfile.write(body)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            argv=[str(binary),'--check','--check-port',str(server.server_address[1])]
            run(argv)
            for scenario in ('redirect','oversized','streamed_oversized'):
                result=subprocess.run(argv,cwd=ROOT,capture_output=True,timeout=12)
                if result.returncode!=1:raise RuntimeError('Native transport failed to reject '+scenario+'.')
        finally:server.shutdown();server.server_close();thread.join(timeout=2)
        build=run(['otool','-l',str(binary)]).stdout
        if not any('minos 13.0' in line for line in build.splitlines()):raise RuntimeError('Native deployment target does not match macOS 13.')
        return {'architecture':machine,'target':target,'binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),
                'freshness_self_test':True,'widget_layout_test':True,'shared_selection_fixture':True,'synthetic_decoder':True,
                'redirect_and_body_limits':True}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--receipt',type=Path)
    args=parser.parse_args()
    before=build_manifest(ROOT);validation=validation_fingerprint();started=time.monotonic()
    result=run(['make','check','PYTHON='+sys.executable])
    native=native_gate()
    after=build_manifest(ROOT)
    if before!=after or validation!=validation_fingerprint():raise RuntimeError('Source or validation inputs changed during release validation; rerun the gate.')
    receipt={'schema':1,'source_fingerprint':before['source_fingerprint'],'version':before['version'],
        'validation_fingerprint':validation,
        'checked_at':datetime.now(timezone.utc).isoformat(),'elapsed_seconds':round(time.monotonic()-started,3),
        'local_gates':True,'native':native,'live_provider_qualification':False,'installation_qualification':False}
    destination=args.receipt or ROOT/'work/release-validation'/before['source_fingerprint']/'gate.json'
    destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    destination.write_text(json.dumps(receipt,sort_keys=True,indent=2)+'\n');destination.chmod(0o600)
    print('Local release gates passed for '+before['source_fingerprint'])
    print('Receipt: '+str(destination))


if __name__=='__main__':
    try:main()
    except (OSError,ValueError,RuntimeError) as error:raise SystemExit(str(error))
