#!/usr/bin/env python3
"""Install the local plan dashboard at login; no credentials copied or CI triggered."""
from pathlib import Path
import os
import plistlib
import subprocess
import sys
import time
import urllib.request

root=Path(__file__).resolve().parents[1]
data=Path.home()/'.local/share/aiusage';data.mkdir(parents=True,exist_ok=True,mode=0o700);data.chmod(0o700)
runtime=Path('/opt/homebrew/opt/python@3.11/libexec/bin/python3')
if not runtime.exists():runtime=Path(sys.executable)
label='com.xxx.aiusage'
target=Path.home()/'Library/LaunchAgents'/f'{label}.plist';target.parent.mkdir(parents=True,exist_ok=True)
log=data/'service.log';log.touch(mode=0o600,exist_ok=True)
spec={'Label':label,'ProgramArguments':[str(runtime),'-m','aiusage.server'],
      'WorkingDirectory':str(root),'EnvironmentVariables':{'PYTHONPATH':str(root/'src')},
      'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':10,'ProcessType':'Background','Umask':0o077,
      'StandardOutPath':str(log),'StandardErrorPath':str(log)}
target.write_bytes(plistlib.dumps(spec));target.chmod(0o600)
domain=f'gui/{os.getuid()}'
subprocess.run(['/bin/launchctl','bootout',domain+'/'+label],capture_output=True)
for _ in range(20):
    result=subprocess.run(['/bin/launchctl','bootstrap',domain,str(target)],capture_output=True)
    if result.returncode==0:break
    time.sleep(.2)
else:raise SystemExit('Headroom service registration failed')
subprocess.run(['/bin/launchctl','kickstart',domain+'/'+label],check=True)
for _ in range(50):
    try:
        with urllib.request.urlopen('http://127.0.0.1:8793/health',timeout=2) as response:
            if response.status==200:break
    except OSError:pass
    time.sleep(.2)
else:raise SystemExit('Service registered but health endpoint is unavailable; inspect private service log')
print('Headroom installed: http://127.0.0.1:8793/')
