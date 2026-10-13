#!/usr/bin/env python3
"""Print only allowlisted local health fields, never a launchd environment."""
import json
import os
import re
import subprocess
import urllib.request
import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import importlib


def agent_status(label):
    result=subprocess.run(['/bin/launchctl','print',f'gui/{os.getuid()}/{label}'],capture_output=True,timeout=5)
    if result.returncode:return {'registered':False,'running':False}
    raw=result.stdout.decode(errors='replace')
    state=re.search(r'^\s*state = (running|waiting|not running)\s*$',raw,re.M)
    pid=re.search(r'^\s*pid = (\d+)\s*$',raw,re.M)
    return {'registered':True,'running':bool(state and state[1]=='running'),
            'pid':int(pid[1]) if pid else None}


def probe(provider=None):
    """Exercise built-in collectors without starting a service or retaining replies."""
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
    from aiusage.store import Store
    from aiusage.registry import PROVIDERS
    if provider is not None and provider not in PROVIDERS:raise ValueError('Unknown provider')
    with tempfile.TemporaryDirectory(prefix='headroom-diagnostics-') as directory:
        store=Store(directory)
        provider_ids=[provider] if provider else list(PROVIDERS)
        def fetch(provider_id):
            try:
                module=importlib.import_module('aiusage.'+provider_id)
                store.save_provider(module.collect(store))
            except Exception:
                store.save_provider({'id':provider_id,'status':'error','windows':[],
                    'source':'built_in_probe','error':{'code':'collector_error','message':'Provider probe failed.'}})
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(fetch,provider_ids))
        data=store.portfolio()
        return {'mode':'built_in_probe','providers':[{'id':p['id'],'status':p['status'],
            'source':p['source'],'window_count':len(p['windows']),
            'fresh_window_count':sum(w['status']=='fresh' for w in p['windows']),
            'error_code':p['error']['code'] if p['error'] else None} for p in data['providers'] if p['id'] in provider_ids]}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--probe',action='store_true',help='Check built-in collectors in isolated temporary storage')
    parser.add_argument('--provider',choices=('codex','cursor','zai','antigravity','muse','claude'),help='Limit --probe to one provider')
    options=parser.parse_args()
    if options.provider and not options.probe:parser.error('--provider requires --probe')
    if options.probe:
        print(json.dumps(probe(options.provider),indent=2));return
    report={'service':agent_status('com.xxx.aiusage'),'menu':agent_status('com.xxx.codex-usage-menu')}
    try:
        native=json.loads((Path.home()/'.local/share/aiusage/native-status.json').read_text())
        report['native']={key:native.get(key) for key in ('widget_visible','stale','providers','measured_providers')}
    except (OSError,ValueError,AttributeError):pass
    try:
        with urllib.request.urlopen('http://127.0.0.1:8793/health',timeout=5) as response:data=json.load(response)
        report['refresh_seconds']=data['refresh_interval_seconds']
        report['max_age_seconds']=data['freshness_policy']['max_age_seconds']
        report['providers']=[{'id':p['id'],'status':p['status'],'source':p.get('source'), 'remaining_pct':p['remaining_pct'],
            'error_code':p['error']['code'] if p['error'] else None} for p in data['providers']]
    except Exception:report['api']='unavailable'
    print(json.dumps(report,indent=2))

if __name__=='__main__':main()
