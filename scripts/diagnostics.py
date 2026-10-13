#!/usr/bin/env python3
"""Print only allowlisted local health fields, never a launchd environment."""
import json
import os
import re
import subprocess
import urllib.request


def agent_status(label):
    result=subprocess.run(['/bin/launchctl','print',f'gui/{os.getuid()}/{label}'],capture_output=True,timeout=5)
    if result.returncode:return {'registered':False,'running':False}
    raw=result.stdout.decode(errors='replace')
    state=re.search(r'^\s*state = (running|waiting|not running)\s*$',raw,re.M)
    pid=re.search(r'^\s*pid = (\d+)\s*$',raw,re.M)
    return {'registered':True,'running':bool(state and state[1]=='running'),
            'pid':int(pid[1]) if pid else None}


def main():
    report={'service':agent_status('com.xxx.aiusage'),'menu':agent_status('com.xxx.codex-usage-menu')}
    try:
        with urllib.request.urlopen('http://127.0.0.1:8793/health',timeout=5) as response:data=json.load(response)
        report['refresh_seconds']=data['refresh_interval_seconds']
        report['max_age_seconds']=data['freshness_policy']['max_age_seconds']
        report['providers']=[{'id':p['id'],'status':p['status'],'remaining_pct':p['remaining_pct'],
            'error_code':p['error']['code'] if p['error'] else None} for p in data['providers']]
    except Exception:report['api']='unavailable'
    print(json.dumps(report,indent=2))

if __name__=='__main__':main()
