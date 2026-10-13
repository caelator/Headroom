"""Transactional per-user installation; preserve data and previous artifacts."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
sys.path.insert(0,str(ROOT/'scripts'))
from aiusage.release import snapshot,verify_payload
from aiusage.provider_http import NoRedirect
from diagnostics import agent_status


class InstallError(Exception):pass


def private_directory(path):
    path=Path(path)
    if path.is_symlink():raise InstallError('Installation directory must not be a symbolic link.')
    path.mkdir(parents=True,exist_ok=True,mode=0o700);path.chmod(0o700)
    return path


def atomic_write(path,data,mode=0o600):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    if path.is_symlink():raise InstallError('Installation metadata must not be a symbolic link.')
    fd,name=tempfile.mkstemp(prefix='.'+path.name+'-',dir=path.parent)
    try:
        os.fchmod(fd,mode)
        with os.fdopen(fd,'wb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.replace(name,path)
    except BaseException:
        try:os.unlink(name)
        except FileNotFoundError:pass
        raise


@contextmanager
def installation_lock(data):
    data=private_directory(data)
    fd=os.open(data/'install.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        os.fchmod(fd,0o600)
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise InstallError('Another Headroom installation is active.') from None
        yield
    finally:os.close(fd)


def command(argv,check=True,timeout=30):
    result=subprocess.run(argv,capture_output=True,timeout=timeout,check=False)
    if check and result.returncode:raise InstallError('Installation command failed; the previous installation will be restored.')
    return result


def bootout(label):command(['/bin/launchctl','bootout',f'gui/{os.getuid()}/{label}'],check=False)


def bootstrap(label,plist):
    domain=f'gui/{os.getuid()}'
    for _ in range(20):
        if command(['/bin/launchctl','bootstrap',domain,str(plist)],check=False).returncode==0:break
        time.sleep(.2)
    else:raise InstallError('LaunchAgent registration failed.')
    command(['/bin/launchctl','kickstart',domain+'/'+label])


def health(expected=None,expected_pid=None):
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    with opener.open('http://127.0.0.1:8793/health',timeout=3) as response:
        raw=response.read(1024*1024+1)
        if len(raw)>1024*1024:raise InstallError('Local health response exceeds the limit.')
        result=json.loads(raw)
    identity=result.get('service') or {}
    if identity.get('name')!='headroom' or identity.get('api_version')!=1:raise InstallError('Unexpected service on the dashboard port.')
    if expected and identity.get('source_fingerprint')!=expected:raise InstallError('The running service does not match the installed build.')
    if expected_pid and identity.get('pid')!=expected_pid:raise InstallError('Dashboard response belongs to another process.')
    if not isinstance(result.get('providers'),list) or len(result['providers'])!=6:raise InstallError('Provider registry is incomplete.')
    return result


def wait_service(label,fingerprint,timeout=20):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        state=agent_status(label)
        try:
            if state['running']:
                result=health(fingerprint,state['pid'])
                socket=command(['/usr/sbin/lsof','-a','-nP','-p',str(state['pid']),'-iTCP:8793','-sTCP:LISTEN','-Fpn'],check=False,timeout=3)
                lines=socket.stdout.decode(errors='replace').splitlines()
                if socket.returncode==0 and f'p{state["pid"]}' in lines and 'n127.0.0.1:8793' in lines:return result
        except (OSError,ValueError,InstallError):pass
        time.sleep(.2)
    raise InstallError('Installed service did not establish verified loopback ownership.')


def stage_release(root,data):return snapshot(root,private_directory(Path(data)/'releases'))


def file_hash(path):
    path=Path(path)
    if path.is_symlink():raise InstallError('Managed installation files must not be symbolic links.')
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def bundle_hash(path):
    path=Path(path)
    if not path.exists():return None
    digest=hashlib.sha256()
    for file in sorted(path.rglob('*')):
        if file.is_symlink():raise InstallError('Managed bundle must not contain symbolic links.')
        if file.is_file():digest.update(file.relative_to(path).as_posix().encode()+b'\0'+file.read_bytes())
    return digest.hexdigest()


def _journal(data,value):atomic_write(Path(data)/'pending-install.json',json.dumps(value,sort_keys=True).encode())


def recover(data):
    """The caller holds the live kernel lock; a prior transaction is no longer active."""
    path=Path(data)/'pending-install.json'
    if not path.exists():return
    value=json.loads(path.read_text());plist=Path(value['plist']);candidate=value['candidate_plist_hash']
    current=file_hash(plist)
    if current not in (value['previous_plist_hash'],candidate,None):
        raise InstallError('Installation recovery conflicts with a changed LaunchAgent; review the preserved backup.')
    before=Path(value['plist_backup']) if value.get('plist_backup') else None
    if before and before.exists() and file_hash(before)!=value['previous_plist_hash']:
        raise InstallError('Preserved LaunchAgent backup changed; recovery requires review.')
    receipt_backup=Path(value['receipt_backup']) if value.get('receipt_backup') else None
    if receipt_backup and receipt_backup.exists() and file_hash(receipt_backup)!=value['previous_receipt_hash']:
        raise InstallError('Preserved installation receipt changed; recovery requires review.')
    bootout(value['label'])
    bundle=Path(value['bundle']) if value.get('bundle') else None
    backup=Path(value['bundle_backup']) if value.get('bundle_backup') else None
    if bundle and bundle_hash(bundle) not in (value['previous_bundle_hash'],value['candidate_bundle_hash'],None):
        raise InstallError('Installation recovery conflicts with a changed application; review the preserved backup.')
    if backup and backup.exists() and bundle_hash(backup)!=value['previous_bundle_hash']:
        raise InstallError('Preserved application backup changed; recovery requires review.')
    if bundle and backup and backup.exists():
        if bundle.exists():bundle.rename(bundle.parent/('.Headroom.failed-'+value['id']+'.app'))
        backup.rename(bundle)
    elif bundle and not value.get('previous_bundle') and bundle.exists():
        bundle.rename(bundle.parent/('.Headroom.failed-'+value['id']+'.app'))
    if before and before.exists():os.replace(before,plist)
    elif value['previous_plist_hash'] is None and plist.exists():plist.unlink()
    if value['previous_registered'] and plist.exists():bootstrap(value['label'],plist)
    receipt=Path(data)/'installation.json'
    if receipt_backup and receipt_backup.exists():os.replace(receipt_backup,receipt)
    elif value.get('previous_receipt_hash') is None and receipt.exists():receipt.unlink()
    path.unlink()


def activate(label,plist,spec,data,validate,bundle=None,candidate_bundle=None,complete=None):
    """Prepare backups, activate, verify, then record completion; roll back on failure."""
    plist=Path(plist);data=Path(data);identifier=uuid.uuid4().hex
    old_state=agent_status(label);previous=file_hash(plist);payload=plistlib.dumps(spec)
    archive=private_directory(plist.parent/'.headroom-backups'/identifier)
    before=archive/plist.name if previous is not None else None
    if before:os.link(plist,before)  # Retain the original inode, without extracting/copying environment values.
    receipt_path=data/'installation.json';previous_receipt=file_hash(receipt_path)
    receipt_backup=None
    if previous_receipt is not None:
        receipt_archive=private_directory(data/'.install-backups'/identifier)
        receipt_backup=receipt_archive/'installation.json';os.link(receipt_path,receipt_backup)
    bundle=Path(bundle) if bundle else None
    candidate_bundle=Path(candidate_bundle) if candidate_bundle else None
    previous_bundle=bool(bundle and bundle.exists())
    bundle_backup=None
    if bundle:
        if bundle.is_symlink():raise InstallError('Managed application bundle must not be a symbolic link.')
        bundle_archive=private_directory(bundle.parent/'.headroom-backups'/identifier)
        bundle_backup=bundle_archive/bundle.name
    journal={'id':identifier,'label':label,'plist':str(plist),'plist_backup':str(before) if before else None,
        'previous_plist_hash':previous,'candidate_plist_hash':hashlib.sha256(payload).hexdigest(),
        'previous_registered':old_state['registered'],'bundle':str(bundle) if bundle else None,
        'bundle_backup':str(bundle_backup) if bundle_backup else None,'previous_bundle':previous_bundle,
        'previous_bundle_hash':bundle_hash(bundle) if bundle else None,
        'candidate_bundle_hash':bundle_hash(candidate_bundle) if candidate_bundle else None,
        'previous_receipt_hash':previous_receipt,'receipt_backup':str(receipt_backup) if receipt_backup else None}
    _journal(data,journal)
    try:
        bootout(label)
        if bundle:
            if previous_bundle:bundle.rename(bundle_backup)
            candidate_bundle.rename(bundle)
        atomic_write(plist,payload)
        bootstrap(label,plist)
        validate()
        result={'label':label,'plist_backup':str(before) if before else None,
                'bundle_backup':str(bundle_backup) if previous_bundle else None}
        if complete:complete(result)
        (data/'pending-install.json').unlink()
        return result
    except BaseException:
        try:recover(data)
        except Exception:raise InstallError('Installation failed and rollback needs recovery from the preserved backup.') from None
        raise InstallError('Installation failed; the previous installation was restored.') from None


def record_installation(data,component,record):
    path=Path(data)/'installation.json'
    try:receipt=json.loads(path.read_text())
    except (OSError,ValueError):receipt={'schema':1}
    receipt[component]=record
    atomic_write(path,json.dumps(receipt,sort_keys=True,indent=2).encode())
