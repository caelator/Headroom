"""Code-only release fingerprints and immutable installation payloads."""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import tomllib

API_VERSION=1
MANIFEST='headroom-release.json'
ROOTS=('src','web','native','scripts','.githooks','third_party')
TOP_FILES=('pyproject.toml','Makefile','LICENSE','THIRD_PARTY_NOTICES.md')
SUFFIXES={'src':('.py',),'web':('.html','.js','.css','.svg','.png','.jpg','.jpeg','.webp','.woff','.woff2'),
          'native':('.swift',),'scripts':('.py','.sh'),'.githooks':('', '.py','.sh'),'third_party':('.txt','.md')}


def source_files(root):
    root=Path(root).resolve();files=[]
    for name in ROOTS:
        directory=root/name
        if not directory.exists():continue
        if directory.is_symlink():raise ValueError('Release source directories must not be symlinks')
        for path in directory.rglob('*'):
            relative=path.relative_to(root)
            if any(part.startswith('.') for part in relative.parts[1:]):continue
            if '__pycache__' in relative.parts or path.suffix in ('.pyc','.log','.sqlite3'):continue
            if path.is_symlink():raise ValueError('Release source must not be a symlink')
            if path.is_file() and path.suffix in SUFFIXES[name]:files.append(relative.as_posix())
    for name in TOP_FILES:
        path=root/name
        if path.is_symlink():raise ValueError('Release metadata must not be a symlink')
        if path.is_file():files.append(name)
    return sorted(files)


def build_manifest(root):
    root=Path(root).resolve();items=[];digest=hashlib.sha256()
    for name in source_files(root):
        data=(root/name).read_bytes();checksum=hashlib.sha256(data).hexdigest()
        items.append({'path':name,'sha256':checksum})
        digest.update(name.encode()+b'\0'+str(len(data)).encode()+b'\0'+data)
    project=tomllib.loads((root/'pyproject.toml').read_text())['project']
    return {'schema':1,'api_version':API_VERSION,'version':project['version'],
            'source_fingerprint':digest.hexdigest(),'files':items}


def verify_payload(root):
    root=Path(root)
    declared=json.loads((root/MANIFEST).read_text())
    actual=build_manifest(root)
    if declared!=actual:raise ValueError('Installed release payload does not match its manifest')
    return actual


def snapshot(root,releases):
    """Copy code only, never credentials/history; preserve all earlier payloads."""
    root=Path(root).resolve();releases=Path(releases)
    releases.mkdir(parents=True,exist_ok=True,mode=0o700)
    if releases.is_symlink():raise ValueError('Release directory must not be a symlink')
    releases.chmod(0o700)
    manifest=build_manifest(root);target=releases/manifest['source_fingerprint']
    if target.exists():
        if verify_payload(target)!=manifest:raise ValueError('Existing payload fingerprint is inconsistent')
        return target,manifest
    temporary=Path(tempfile.mkdtemp(prefix='.prepare-',dir=releases))
    try:
        for item in manifest['files']:
            destination=temporary/item['path'];destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(root/item['path'],destination)
        (temporary/MANIFEST).write_text(json.dumps(manifest,sort_keys=True,indent=2)+'\n')
        if verify_payload(temporary)!=manifest:raise ValueError('Source changed during release copy')
        for path in temporary.rglob('*'):
            if path.is_file():path.chmod(0o444)
        for path in sorted((p for p in temporary.rglob('*') if p.is_dir()),reverse=True):path.chmod(0o555)
        temporary.chmod(0o555)
        temporary.rename(target)
    except BaseException:
        if temporary.exists():
            for path in temporary.rglob('*'):
                try:path.chmod(0o700 if path.is_dir() else 0o600)
                except OSError:pass
            temporary.chmod(0o700);shutil.rmtree(temporary)
        raise
    return target,manifest


def identity(root):
    root=Path(root)
    return verify_payload(root) if (root/MANIFEST).exists() else build_manifest(root)
