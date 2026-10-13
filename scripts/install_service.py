#!/usr/bin/env python3
"""Install the verified, immutable Headroom service payload at login."""
from pathlib import Path
import os
import plistlib
import sys
from install_support import (ROOT,InstallError,activate,command,installation_lock,
    record_installation,recover,stage_release,wait_service)


def install():
    data=Path.home()/'.local/share/aiusage';label='com.xxx.aiusage'
    with installation_lock(data):
        recover(data);payload,manifest=stage_release(ROOT,data)
        runtime=Path('/opt/homebrew/opt/python@3.11/libexec/bin/python3')
        if not runtime.exists():runtime=Path(sys.executable)
        command([str(runtime),'-c','import sys; assert sys.version_info >= (3,11)'])
        target=Path.home()/'Library/LaunchAgents'/f'{label}.plist';target.parent.mkdir(parents=True,exist_ok=True)
        log=data/'service.log';log.touch(mode=0o600,exist_ok=True);log.chmod(0o600)
        spec={'Label':label,'ProgramArguments':[str(runtime),'-m','aiusage.server','--data-dir',str(data)],
            'WorkingDirectory':str(payload),'EnvironmentVariables':{'PYTHONPATH':str(payload/'src'),'PYTHONDONTWRITEBYTECODE':'1'},
            'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':10,'ProcessType':'Background','Umask':0o077,
            'StandardOutPath':str(log),'StandardErrorPath':str(log)}
        def complete(receipt):
            record_installation(data,'service',{'source_fingerprint':manifest['source_fingerprint'],
                'payload':str(payload),'runtime':str(runtime),'version':manifest['version'],**receipt})
        activate(label,target,spec,data,lambda:wait_service(label,manifest['source_fingerprint']),complete=complete)
        print('Headroom service installed and verified: http://127.0.0.1:8793/')
        print('Source fingerprint: '+manifest['source_fingerprint'])


if __name__=='__main__':
    try:install()
    except (InstallError,OSError,ValueError):raise SystemExit('Headroom service installation failed; previous artifacts are preserved. Use diagnostics and private backups for recovery.')
