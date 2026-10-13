#!/usr/bin/env python3
"""Build, verify and transactionally install Headroom's native companion."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import tempfile
import time
import uuid
from install_support import (ROOT,InstallError,activate,agent_status,bundle_hash,command,health,
    installation_lock,record_installation,recover,stage_release)

BUNDLE_ID='com.caelator.headroom'


def wait_native(label,fingerprint,data,timeout=20):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        state=agent_status(label)
        try:
            status=json.loads((data/'native-status.json').read_text())
            if (state['running'] and status.get('pid')==state['pid'] and
                status.get('source_fingerprint')==fingerprint and not status.get('stale') and
                status.get('providers')==6 and 0<=time.time()-status.get('updated_at',0)<15):return
        except (OSError,ValueError,TypeError):pass
        time.sleep(.2)
    raise InstallError('Native companion did not establish verified fresh service binding.')


def install(show_widget=False):
    data=Path.home()/'.local/share/aiusage';label='com.xxx.codex-usage-menu'
    applications=Path.home()/'Applications';applications.mkdir(parents=True,exist_ok=True)
    with installation_lock(data):
        recover(data);payload,manifest=stage_release(ROOT,data)
        health(manifest['source_fingerprint'])
        machine=platform.machine()
        if machine not in ('arm64','x86_64'):raise InstallError('Unsupported native platform architecture.')
        target=machine+'-apple-macosx13.0'
        temporary=Path(tempfile.mkdtemp(prefix='.Headroom.prepare-',dir=applications))
        try:
            candidate=temporary/'Headroom.app';binary=candidate/'Contents/MacOS/Headroom'
            binary.parent.mkdir(parents=True)
            info={'CFBundleIdentifier':BUNDLE_ID,'CFBundleName':'Headroom','CFBundleDisplayName':'Headroom',
                'CFBundleExecutable':'Headroom','CFBundlePackageType':'APPL','CFBundleVersion':str(int(time.time())),
                'CFBundleShortVersionString':manifest['version'],'LSMinimumSystemVersion':'13.0','LSUIElement':True,
                'NSAppTransportSecurity':{'NSAllowsLocalNetworking':True},
                'HeadroomSourceFingerprint':manifest['source_fingerprint'],'HeadroomInstallID':uuid.uuid4().hex,
                'HeadroomShowWidgetAfterInstall':show_widget}
            (candidate/'Contents/Info.plist').write_bytes(plistlib.dumps(info))
            resources=candidate/'Contents/Resources';resources.mkdir()
            for name in ('LICENSE','THIRD_PARTY_NOTICES.md'):
                if (payload/name).exists():shutil.copyfile(payload/name,resources/name)
            if (payload/'third_party').exists():shutil.copytree(payload/'third_party',resources/'third_party')
            command(['/usr/bin/xcrun','swiftc','-O','-swift-version','5','-target',target,
                '-framework','Cocoa',str(payload/'native/MenuBar.swift'),'-o',str(binary)],timeout=180)
            command([str(binary),'--self-test'],timeout=15)
            command([str(binary),'--widget-self-test'],timeout=15)
            command([str(binary),'--check'],timeout=15)
            command(['/usr/bin/codesign','--force','--sign','-',str(candidate)])
            command(['/usr/bin/codesign','--verify','--strict',str(candidate)])
            bundle=applications/'Headroom.app';installed_binary=bundle/'Contents/MacOS/Headroom'
            plist=Path.home()/'Library/LaunchAgents'/f'{label}.plist';plist.parent.mkdir(parents=True,exist_ok=True)
            log=data/'menubar.log';log.touch(mode=0o600,exist_ok=True);log.chmod(0o600)
            spec={'Label':label,'ProgramArguments':[str(installed_binary)],'RunAtLoad':True,
                'KeepAlive':{'SuccessfulExit':False},'ThrottleInterval':10,'ProcessType':'Interactive','Umask':0o077,
                'StandardOutPath':str(log),'StandardErrorPath':str(log)}
            def complete(receipt):
                record_installation(data,'native',{'source_fingerprint':manifest['source_fingerprint'],
                    'binary_sha256':hashlib.sha256(installed_binary.read_bytes()).hexdigest(),
                    'bundle_sha256':bundle_hash(bundle),
                    'bundle_identifier':BUNDLE_ID,'target':target,'version':manifest['version'],**receipt})
            activate(label,plist,spec,data,lambda:wait_native(label,manifest['source_fingerprint'],data),
                bundle=bundle,candidate_bundle=candidate,complete=complete)
        finally:shutil.rmtree(temporary,ignore_errors=True)
    print('Headroom native companion installed and verified.')
    print('Source fingerprint: '+manifest['source_fingerprint'])


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--show-widget',action='store_true',help='Reveal the widget once after this installation')
    args=parser.parse_args()
    try:install(args.show_widget)
    except (InstallError,OSError,ValueError):raise SystemExit('Native installation failed; prior artifacts are preserved. Run diagnostics before retrying.')
