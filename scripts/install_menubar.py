#!/usr/bin/env python3
"""Replace the owned token-only menu companion with the verified-plan indicator."""
import os
from pathlib import Path
import plistlib
import subprocess
import time
import tempfile

root=Path(__file__).resolve().parents[1];data=Path.home()/'.local/share/aiusage'
data.mkdir(parents=True,exist_ok=True,mode=0o700);data.chmod(0o700)
bundle=Path.home()/'Applications/Headroom.app';binary=bundle/'Contents/MacOS/Headroom'
binary.parent.mkdir(parents=True,exist_ok=True)
label='com.xxx.codex-usage-menu';domain=f'gui/{os.getuid()}'
target=Path.home()/'Library/LaunchAgents'/f'{label}.plist'
if target.exists() and not (data/'menubar-agent.before.plist').exists():
    backup=data/'menubar-agent.before.plist';backup.write_bytes(target.read_bytes());backup.chmod(0o600)
info={'CFBundleIdentifier':label,'CFBundleName':'Headroom','CFBundleDisplayName':'Headroom','CFBundleExecutable':'Headroom',
      'CFBundlePackageType':'APPL','CFBundleVersion':'1','CFBundleShortVersionString':'0.1',
      'LSMinimumSystemVersion':'13.0','LSUIElement':True,'NSAppTransportSecurity':{'NSAllowsLocalNetworking':True}}
(bundle/'Contents/Info.plist').write_bytes(plistlib.dumps(info))
with tempfile.TemporaryDirectory(prefix='headroom-native-build-') as temporary:
    candidate=Path(temporary)/'Headroom'
    subprocess.run(['/usr/bin/xcrun','swiftc','-O','-swift-version','5','-framework','Cocoa',str(root/'native/MenuBar.swift'),'-o',str(candidate)],check=True)
    subprocess.run([str(candidate),'--self-test'],check=True)
    subprocess.run([str(candidate),'--check'],check=True)
    # Compile and verify before replacing the owned companion's executable.
    staged=binary.with_name('Headroom.new');staged.write_bytes(candidate.read_bytes());staged.chmod(0o755)
    os.replace(staged,binary)
subprocess.run(['/usr/bin/codesign','--force','--sign','-',str(bundle)],check=True)
subprocess.run([str(binary),'--check'],check=True)
log=data/'menubar.log';log.touch(mode=0o600,exist_ok=True)
spec={'Label':label,'ProgramArguments':[str(binary)],'RunAtLoad':True,'KeepAlive':{'SuccessfulExit':False},
      'ThrottleInterval':10,'ProcessType':'Interactive','Umask':0o077,'StandardOutPath':str(log),'StandardErrorPath':str(log)}
target.write_bytes(plistlib.dumps(spec));target.chmod(0o600)
subprocess.run(['/bin/launchctl','bootout',domain+'/'+label],capture_output=True)
for _ in range(20):
    result=subprocess.run(['/bin/launchctl','bootstrap',domain,str(target)],capture_output=True)
    if result.returncode==0:break
    time.sleep(.2)
else:raise SystemExit('Menu registration failed')
subprocess.run(['/bin/launchctl','kickstart',domain+'/'+label],check=True)
print('Installed Headroom menu indicator and floating desktop widget.')
