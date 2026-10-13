#!/usr/bin/env python3
"""Print only allowlisted local health fields, never a launchd environment."""
import hashlib
import json
import os
import plistlib
import sqlite3
import re
import subprocess
import time
import urllib.request
import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile

SERVICE_LABEL='com.xxx.aiusage'
MENU_LABEL='com.xxx.codex-usage-menu'
NATIVE_BUNDLE_ID='com.caelator.headroom'
_HEX64=re.compile(r'^[0-9a-f]{64}$')
_VERSION=re.compile(r'^[0-9A-Za-z][0-9A-Za-z.+-]{0,31}$')
# Mirrors install_menubar.wait_native: a heartbeat is fresh for 15 seconds.
HEARTBEAT_FRESH_SECONDS=15
PANEL_VISIBLE_EVIDENCE='panel_is_visible_flag_only_not_unobscured_desktop_proof'


def _is_fingerprint(value):
    return isinstance(value,str) and bool(_HEX64.match(value))


def _is_version(value):
    return isinstance(value,str) and bool(_VERSION.match(value))


def _is_pid(value):
    return isinstance(value,int) and not isinstance(value,bool) and value>0


def _is_count(value):
    return isinstance(value,int) and not isinstance(value,bool) and value>=0


def agent_status(label):
    result=subprocess.run(['/bin/launchctl','print',f'gui/{os.getuid()}/{label}'],capture_output=True,timeout=5)
    if result.returncode:return {'registered':False,'running':False}
    raw=result.stdout.decode(errors='replace')
    state=re.search(r'^\s*state = (running|waiting|not running)\s*$',raw,re.M)
    pid=re.search(r'^\s*pid = (\d+)\s*$',raw,re.M)
    return {'registered':True,'running':bool(state and state[1]=='running'),
            'pid':int(pid[1]) if pid else None}


def listen_status(pid,runner=None):
    """Verify, via lsof field output, that pid owns the TCP 8793 listener.

    Only the parsed pid/listening booleans leave this function; raw lsof
    output (paths, sockets) is never returned.
    """
    result={'checked':False,'listening':False,'pid_matches_service':False}
    if not _is_pid(pid):return result
    run=runner or subprocess.run
    try:
        completed=run(['/usr/sbin/lsof','-a','-nP','-p',str(pid),'-iTCP:8793','-sTCP:LISTEN','-Fpn'],
                      capture_output=True,timeout=3,check=False)
    except Exception:return result
    result['checked']=True
    stdout=getattr(completed,'stdout',b'') or b''
    if isinstance(stdout,bytes):stdout=stdout.decode(errors='replace')
    if not isinstance(stdout,str):return result
    lines=stdout.splitlines()
    result['pid_matches_service']=f'p{pid}' in lines
    result['listening']=bool(getattr(completed,'returncode',1)==0 and result['pid_matches_service'] and
        'n127.0.0.1:8793' in lines)
    return result


def payload_status(payload,expected_fingerprint=None):
    """Verify an installed payload against its own allowlisted manifest.

    Recomputes each listed file hash and the release fingerprint using the
    algorithm in aiusage.release.build_manifest, without importing or
    executing code from the payload being checked. Only the fingerprint,
    version and counts are returned; file contents and paths beyond a
    directory-name comparison are never returned.
    """
    result={'present':False,'manifest_valid':False,'source_fingerprint':None,'version':None,
            'file_count':0,'matches_receipt':False,'directory_matches_fingerprint':False}
    if payload is None:return result
    payload=Path(payload)
    manifest_path=payload/'headroom-release.json'
    if payload.is_symlink() or not manifest_path.is_file() or manifest_path.is_symlink():return result
    result['present']=True
    try:
        declared=json.loads(manifest_path.read_text())
    except (OSError,ValueError):return result
    if not isinstance(declared,dict):return result
    fingerprint=declared.get('source_fingerprint');files=declared.get('files')
    if _is_fingerprint(fingerprint):result['source_fingerprint']=fingerprint
    if _is_version(declared.get('version')):result['version']=declared['version']
    result['directory_matches_fingerprint']=bool(_is_fingerprint(fingerprint) and payload.name==fingerprint)
    result['matches_receipt']=bool(_is_fingerprint(fingerprint) and fingerprint==expected_fingerprint)
    if not _is_fingerprint(fingerprint) or not isinstance(files,list) or not files:return result
    digest=hashlib.sha256()
    try:
        for item in files:
            if not isinstance(item,dict):return result
            name=item.get('path');expected_hash=item.get('sha256')
            if not isinstance(name,str) or not name or not _is_fingerprint(expected_hash):return result
            relative=Path(name)
            if relative.is_absolute() or '..' in relative.parts or not relative.parts:return result
            path=payload/relative
            if path.is_symlink() or not path.is_file():return result
            data=path.read_bytes()
            if hashlib.sha256(data).hexdigest()!=expected_hash:return result
            digest.update(name.encode()+b'\0'+str(len(data)).encode()+b'\0'+data)
    except OSError:return result
    result['file_count']=len(files)
    try:
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
        from aiusage.release import build_manifest
        actual=build_manifest(payload)
    except (OSError,ValueError,KeyError):return result
    result['manifest_valid']=bool(digest.hexdigest()==fingerprint and declared==actual)
    return result


def _bundle_hash(bundle):
    """Hash a bundle exactly as install_support.bundle_hash does."""
    digest=hashlib.sha256()
    try:
        for path in sorted(Path(bundle).rglob('*')):
            if path.is_symlink():return None
            if path.is_file():digest.update(path.relative_to(bundle).as_posix().encode()+b'\0'+path.read_bytes())
    except OSError:return None
    return digest.hexdigest()


def _read_receipt(data):
    """Extract only allowlisted receipt fields; never echo the raw receipt."""
    empty={'service_fingerprint':None,'service_payload':None,'service_version':None,
           'native_fingerprint':None,'native_binary_sha256':None,'native_bundle_sha256':None,'native_bundle_id':None,'native_version':None}
    try:receipt=json.loads((Path(data)/'installation.json').read_text())
    except (OSError,ValueError):return empty
    if not isinstance(receipt,dict):return empty
    service=receipt.get('service') if isinstance(receipt.get('service'),dict) else {}
    native=receipt.get('native') if isinstance(receipt.get('native'),dict) else {}
    return {'service_fingerprint':service.get('source_fingerprint') if _is_fingerprint(service.get('source_fingerprint')) else None,
            'service_payload':service.get('payload') if isinstance(service.get('payload'),str) else None,
            'service_version':service.get('version') if _is_version(service.get('version')) else None,
            'native_fingerprint':native.get('source_fingerprint') if _is_fingerprint(native.get('source_fingerprint')) else None,
            'native_binary_sha256':native.get('binary_sha256') if _is_fingerprint(native.get('binary_sha256')) else None,
            'native_bundle_sha256':native.get('bundle_sha256') if _is_fingerprint(native.get('bundle_sha256')) else None,
            'native_bundle_id':native.get('bundle_identifier') if native.get('bundle_identifier')==NATIVE_BUNDLE_ID else None,
            'native_version':native.get('version') if _is_version(native.get('version')) else None}


def _read_native_status(data,now=None):
    """Sanitize native-status.json, including panel_visible when reported.

    panel_visible is the native panel's own isVisible flag. It is reported
    as panel-reported visibility only; it does not prove the panel is
    unobscured on the desktop. The legacy widget_visible key is accepted
    as the same panel evidence when panel_visible is absent.
    """
    result={'present':False,'pid':None,'source_fingerprint':None,'stale':None,'providers':None,
            'measured_providers':None,'widget_visible':None,'panel_visible':None,
            'panel_visible_evidence':None,'heartbeat_age_seconds':None,'heartbeat_fresh':False}
    try:native=json.loads((Path(data)/'native-status.json').read_text())
    except (OSError,ValueError):return result
    if not isinstance(native,dict):return result
    result['present']=True
    if _is_pid(native.get('pid')):result['pid']=native['pid']
    if _is_fingerprint(native.get('source_fingerprint')):result['source_fingerprint']=native['source_fingerprint']
    if isinstance(native.get('stale'),bool):result['stale']=native['stale']
    if _is_count(native.get('providers')):result['providers']=native['providers']
    if _is_count(native.get('measured_providers')):result['measured_providers']=native['measured_providers']
    panel=native.get('panel_visible') if isinstance(native.get('panel_visible'),bool) else None
    legacy=native.get('widget_visible') if isinstance(native.get('widget_visible'),bool) else None
    result['widget_visible']=legacy if legacy is not None else panel
    result['panel_visible']=panel if panel is not None else legacy
    if result['panel_visible'] is not None:result['panel_visible_evidence']=PANEL_VISIBLE_EVIDENCE
    updated=native.get('updated_at')
    if isinstance(updated,(int,float)) and not isinstance(updated,bool):
        age=(time.time() if now is None else now)-float(updated)
        if age>=0:
            result['heartbeat_age_seconds']=round(age,3)
            result['heartbeat_fresh']=bool(age<HEARTBEAT_FRESH_SECONDS)
    return result


def health_identity(health):
    """Extract only the allowlisted service identity from a health reply."""
    result={'present':False,'identity_valid':False,'name':None,'api_version':None,'version':None,
            'source_fingerprint':None,'pid':None}
    if not isinstance(health,dict):return result
    service=health.get('service')
    if not isinstance(service,dict):return result
    result['present']=True
    if service.get('name')=='headroom':result['name']='headroom'
    if isinstance(service.get('api_version'),int) and not isinstance(service.get('api_version'),bool) and service.get('api_version')==1:result['api_version']=1
    if _is_version(service.get('version')):result['version']=service['version']
    if _is_fingerprint(service.get('source_fingerprint')):result['source_fingerprint']=service['source_fingerprint']
    if _is_pid(service.get('pid')):result['pid']=service['pid']
    result['identity_valid']=bool(result['name']=='headroom' and result['api_version']==1 and
                                  result['source_fingerprint'] and result['pid'])
    return result


def installed_report(home=None,now=None,runner=None,service_status=None,menu_status=None,health=None):
    """Allowlisted installed-integrity checks for payload, service and native app.

    All inputs can be injected (synthetic homes, fake command runners, fake
    launchd/health values) so tests never touch live credentials or data.
    Nothing here reads a launchd environment, a credential store, or a raw
    account response, and no raw file, plist, receipt or command output is
    returned.
    """
    home=Path.home() if home is None else Path(home)
    data=home/'.local/share/aiusage'
    receipt=_read_receipt(data)
    # Only trust a receipt payload path inside the managed releases tree.
    payload=None
    candidate=receipt['service_payload']
    if candidate:
        path=Path(candidate)
        try:
            if not path.is_symlink() and path.resolve().is_relative_to((data/'releases').resolve()):payload=path
        except (OSError,RuntimeError):payload=None
    if payload is None and receipt['service_fingerprint']:
        fallback=data/'releases'/receipt['service_fingerprint']
        if fallback.is_dir() and not fallback.is_symlink():payload=fallback
    payload_info=payload_status(payload,receipt['service_fingerprint'])
    if receipt['service_version'] and payload_info['version'] is None:payload_info['version']=receipt['service_version']

    service_status=service_status if service_status is not None else agent_status(SERVICE_LABEL)
    menu_status=menu_status if menu_status is not None else agent_status(MENU_LABEL)
    identity=health_identity(health)
    listen=listen_status(identity['pid'],runner=runner)
    service={'registered':bool(service_status.get('registered')),'running':bool(service_status.get('running')),
             'pid':service_status.get('pid') if _is_pid(service_status.get('pid')) else None,
             'health_identity_valid':identity['identity_valid'],'health_pid':identity['pid'],
             'health_pid_matches_launchd':bool(identity['pid'] and service_status.get('pid')==identity['pid']),
             'source_fingerprint':identity['source_fingerprint'],'version':identity['version'],
             'matches_payload':bool(identity['source_fingerprint'] and payload_info['source_fingerprint'] and
                                    identity['source_fingerprint']==payload_info['source_fingerprint']),
             'matches_receipt':bool(identity['source_fingerprint'] and receipt['service_fingerprint'] and
                                    identity['source_fingerprint']==receipt['service_fingerprint']),
             'listen_owner_verified':listen['listening'],'listen_checked':listen['checked'],
             'listen_pid_matches_service':listen['pid_matches_service']}

    bundle=home/'Applications/Headroom.app'
    binary=bundle/'Contents/MacOS/Headroom'
    binary_hash=None;bundle_info={'bundle_present':bool(bundle.is_dir() and not bundle.is_symlink()),
        'binary_present':False,'binary_sha256':None,'binary_matches_receipt':False,'bundle_sha256':None,
        'bundle_source_fingerprint':None,'bundle_identifier_valid':False,'bundle_matches_receipt':False,'version':receipt['native_version']}
    if bundle_info['bundle_present']:
        bundle_info['bundle_sha256']=_bundle_hash(bundle)
        bundle_info['bundle_matches_receipt']=bool(bundle_info['bundle_sha256'] and bundle_info['bundle_sha256']==receipt['native_bundle_sha256'])
        if binary.is_file() and not binary.is_symlink():
            bundle_info['binary_present']=True
            try:binary_hash=hashlib.sha256(binary.read_bytes()).hexdigest()
            except OSError:binary_hash=None
            bundle_info['binary_sha256']=binary_hash
            bundle_info['binary_matches_receipt']=bool(binary_hash and receipt['native_binary_sha256'] and
                                                       binary_hash==receipt['native_binary_sha256'])
        try:
            info=plistlib.loads((bundle/'Contents/Info.plist').read_bytes())
        except Exception:info=None
        if isinstance(info,dict):
            if _is_fingerprint(info.get('HeadroomSourceFingerprint')):
                bundle_info['bundle_source_fingerprint']=info['HeadroomSourceFingerprint']
            bundle_info['bundle_identifier_valid']=bool(info.get('CFBundleIdentifier')==NATIVE_BUNDLE_ID and
                                                        receipt['native_bundle_id']==NATIVE_BUNDLE_ID)
            if _is_version(info.get('CFBundleShortVersionString')):bundle_info['version']=info['CFBundleShortVersionString']
    native_status=_read_native_status(data,now=now)
    native={**bundle_info,
            'registered':bool(menu_status.get('registered')),'running':bool(menu_status.get('running')),
            'launchd_pid':menu_status.get('pid') if _is_pid(menu_status.get('pid')) else None,
            'status_present':native_status['present'],'status_pid':native_status['pid'],
            'status_pid_matches_launchd':bool(native_status['pid'] and menu_status.get('pid')==native_status['pid']),
            'heartbeat_age_seconds':native_status['heartbeat_age_seconds'],'heartbeat_fresh':native_status['heartbeat_fresh'],
            'stale':native_status['stale'],'providers':native_status['providers'],
            'measured_providers':native_status['measured_providers'],
            'widget_visible':native_status['widget_visible'],'panel_visible':native_status['panel_visible'],
            'panel_visible_evidence':native_status['panel_visible_evidence'],
            'source_fingerprint':native_status['source_fingerprint'],
            'matches_payload':bool(native_status['source_fingerprint'] and payload_info['source_fingerprint'] and
                                    native_status['source_fingerprint']==payload_info['source_fingerprint']),
            'bundle_matches_payload':bool(bundle_info['bundle_source_fingerprint'] and payload_info['source_fingerprint'] and
                                          bundle_info['bundle_source_fingerprint']==payload_info['source_fingerprint']),
            'matches_receipt':bool(native_status['source_fingerprint'] and receipt['native_fingerprint'] and
                                   native_status['source_fingerprint']==receipt['native_fingerprint'])}
    fingerprints=[value for value in (payload_info['source_fingerprint'],identity['source_fingerprint'],
                                      native_status['source_fingerprint'],bundle_info['bundle_source_fingerprint']) if value]
    fingerprints_match=bool(len(fingerprints)==4 and
                            len(set(fingerprints))==1)
    consistent=bool(payload_info['manifest_valid'] and payload_info['matches_receipt'] and payload_info['directory_matches_fingerprint'] and
                    service['registered'] and service['running'] and native['registered'] and native['running'] and
                    service['health_identity_valid'] and service['health_pid_matches_launchd'] and
                    service['listen_owner_verified'] and service['matches_payload'] and service['matches_receipt'] and
                    native['binary_matches_receipt'] and native['bundle_matches_receipt'] and native['bundle_identifier_valid'] and native['matches_receipt'] and
                    native['status_pid_matches_launchd'] and native['heartbeat_fresh'] and
                    native['bundle_matches_payload'] and native['matches_payload'] and fingerprints_match)
    return {'payload':payload_info,'service':service,'native':native,
            'fingerprints_match':fingerprints_match,'consistent':consistent}


def probe(provider=None,factory=None):
    """Exercise built-in collectors without starting a service or retaining replies."""
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
    from aiusage.store import Store
    from aiusage.registry import PROVIDERS
    from aiusage.collectors import Collectors
    if provider is not None and provider not in PROVIDERS:raise ValueError('Unknown provider')
    with tempfile.TemporaryDirectory(prefix='headroom-diagnostics-') as directory:
        store=Store(directory)
        provider_ids=[provider] if provider else list(PROVIDERS)
        collectors=(factory or Collectors)(store)
        try:
            with ThreadPoolExecutor(max_workers=6) as pool:list(pool.map(collectors.fetch,provider_ids))
        finally:collectors.close()
        data=store.portfolio()
        return {'mode':'built_in_probe','providers':[{'id':p['id'],'status':p['status'],
            'source':p['source'],'window_count':len(p['windows']),
            'fresh_window_count':sum(w['status']=='fresh' for w in p['windows']),
            'error_code':p['error']['code'] if p['error'] else None} for p in data['providers'] if p['id'] in provider_ids]}


def _fetch_local_json(path):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):return None
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    with opener.open('http://127.0.0.1:8793'+path,timeout=5) as response:
        raw=response.read(1024*1024+1)
        if len(raw)>1024*1024:raise ValueError('Local health response exceeds the limit.')
        return json.loads(raw)


def _fetch_health():
    return _fetch_local_json('/health')


def _fetch_plans():
    return _fetch_local_json('/api/plans')


def quota_report(health, plans):
    """Allowlisted quota fields from the same process as the health identity."""
    identity=health_identity(health)
    if not identity['identity_valid'] or health_identity(plans)!=identity:
        raise ValueError('Service identity changed or could not be verified')
    return {'refresh_seconds':plans['refresh_interval_seconds'],
            'max_age_seconds':plans['freshness_policy']['max_age_seconds'],
            'providers':[{'id':p['id'],'status':p['status'],'source':p.get('source'),
                'remaining_pct':p['remaining_pct'],
                'error_code':p['error']['code'] if p['error'] else None,
                'windows':[{key:w.get(key) for key in ('id','label','status','source',
                    'used_pct','remaining_pct','window_minutes','resets_at','observed_at')}
                    for w in p['windows']]} for p in plans['providers']]}


def storage_status(data):
    """Read-only health/counts; keep damaged history in place for recovery."""
    root=Path(data)
    report={'active_present':False,'active_integrity':False,'archive_files':0,
            'invalid_archives':0,'total_bytes':0,'history_preserved_requires_backup':True}
    files=[root/'plans.sqlite3']
    directory=root/'archives'
    if directory.is_symlink():report['invalid_archives']=1
    elif directory.exists():
        archives=list(directory.glob('????-??.sqlite3'));report['archive_files']=len(archives);files.extend(archives)
    for index,path in enumerate(files):
        if not path.exists():continue
        if index==0:report['active_present']=True
        valid=False;db=None
        try:
            if path.is_symlink():raise ValueError('Invalid history file')
            report['total_bytes']+=path.stat().st_size
            db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)
            valid=db.execute('PRAGMA quick_check').fetchone()[0]=='ok'
            columns={row[1] for row in db.execute('PRAGMA table_info(observations)')}
            valid=valid and {'provider_id','account_ref','window_id','observed_at','remaining_pct','source'}<=columns
        except (OSError,ValueError,sqlite3.Error):valid=False
        finally:
            if db:db.close()
        if index==0:report['active_integrity']=valid
        elif not valid:report['invalid_archives']+=1
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--probe',action='store_true',help='Check built-in collectors in isolated temporary storage')
    parser.add_argument('--provider',choices=('codex','cursor','zai','antigravity','muse','claude'),help='Limit --probe to one provider')
    options=parser.parse_args()
    if options.provider and not options.probe:parser.error('--provider requires --probe')
    if options.probe:
        print(json.dumps(probe(options.provider),indent=2));return
    service_status=agent_status(SERVICE_LABEL);menu_status=agent_status(MENU_LABEL)
    report={'service':service_status,'menu':menu_status}
    report['storage']=storage_status(Path.home()/'.local/share/aiusage')
    native=_read_native_status(Path.home()/'.local/share/aiusage')
    if native['present']:
        report['native']={key:native[key] for key in ('widget_visible','panel_visible','panel_visible_evidence',
            'stale','providers','measured_providers','heartbeat_age_seconds','heartbeat_fresh')}
    health=None
    try:
        health=_fetch_health()
    except Exception:report['api']='unavailable'
    try:
        plans=_fetch_plans()
        # Keep identity qualification separate from a slower quota snapshot.
        # Never associate provider data with a different/restarted service.
        report.update(quota_report(health,plans))
    except Exception:report['quotas']='unavailable'
    report['installed']=installed_report(service_status=service_status,menu_status=menu_status,health=health)
    print(json.dumps(report,indent=2))

if __name__=='__main__':main()
