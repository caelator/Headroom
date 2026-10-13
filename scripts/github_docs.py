#!/usr/bin/env python3
"""Keep public README facts bound to working, staged, or pushed source without CI."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tomllib

BLOCKS=('badges','overview','snapshot','features','requirements','providers','native-note')
SOURCE_ROOTS=('src/','web/','native/','scripts/','.githooks/')
ZERO=re.compile(r'0{40}|0{64}')
OID=re.compile(r'[0-9a-f]{40}|[0-9a-f]{64}')
PREVIEW='docs/assets/preview.json'


class DocsError(Exception):pass


def git(root,*args,input=None):
    result=subprocess.run(['git',*args],cwd=root,input=input,capture_output=True)
    if result.returncode:raise DocsError('Git source snapshot could not be read; resolve index conflicts or invalid revisions.')
    return result.stdout


class Source:
    def __init__(self,root,revision=None):
        self.root=Path(root).resolve();self.revision=revision;self.modes={};self.objects={};self.cache={}
        if revision=='INDEX':
            for row in git(self.root,'ls-files','--stage','-z').split(b'\0'):
                if not row:continue
                meta,path=row.split(b'\t',1);mode,oid,stage=meta.split()
                if stage!=b'0':raise DocsError('Resolve staged merge conflicts before syncing documentation.')
                self.modes[path.decode()]=mode.decode()
                self.objects[path.decode()]=oid.decode()
        elif revision:
            for row in git(self.root,'ls-tree','-r','-z','--full-tree',revision).split(b'\0'):
                if not row:continue
                meta,path=row.split(b'\t',1);mode,kind,_=meta.split()
                if kind==b'blob':self.modes[path.decode()]=mode.decode()
        else:
            for path in git(self.root,'ls-files','--cached','--others','--exclude-standard','-z').split(b'\0'):
                if path:
                    name=path.decode();file=self.root/name
                    if file.is_file():self.modes[name]='120000' if file.is_symlink() else '100644'

    def read(self,path):
        if path not in self.modes:raise DocsError('Required public source is missing: '+path)
        if self.modes[path]=='120000':raise DocsError('Public source and documentation assets must not be symbolic links: '+path)
        if self.revision:
            if path in self.cache:return self.cache[path]
            if self.revision=='INDEX':
                data=git(self.root,'cat-file','blob',self.objects[path])
                self.cache[path]=data;return data
            spec=':'+path if self.revision=='INDEX' else self.revision+':'+path
            data=git(self.root,'show',spec);self.cache[path]=data;return data
        file=self.root/path
        try:file.resolve().relative_to(self.root)
        except ValueError:raise DocsError('Public source must remain inside the repository: '+path) from None
        return file.read_bytes()

    def text(self,path):return self.read(path).decode('utf-8')

    def digest(self,paths):
        value=hashlib.sha256()
        for path in sorted(paths):
            payload=self.read(path)
            value.update(path.encode()+b'\0'+str(len(payload)).encode()+b'\0'+payload)
        return value.hexdigest()


def constants(source,path):
    result={}
    for node in ast.parse(source.text(path)).body:
        if isinstance(node,ast.Assign):
            try:value=ast.literal_eval(node.value)
            except (ValueError,TypeError):continue
            for target in node.targets:
                if isinstance(target,ast.Name):result[target.id]=value
    return result


def facts(source):
    project=tomllib.loads(source.text('pyproject.toml'))['project']
    registry=constants(source,'src/aiusage/registry.py');providers=registry['PROVIDERS']
    if not isinstance(providers,dict) or not providers:raise DocsError('Provider registry must declare its public catalog.')
    wired=set()
    for node in ast.walk(ast.parse(source.text('src/aiusage/collectors.py'))):
        if isinstance(node,ast.Compare) and isinstance(node.left,ast.Name) and node.left.id=='provider':
            wired.update(item.value for item in node.comparators if isinstance(item,ast.Constant) and isinstance(item.value,str))
        if isinstance(node,ast.Dict):
            for key,value in zip(node.keys,node.values):
                if isinstance(key,ast.Constant) and isinstance(value,ast.Name) and value.id=='collect_'+str(key.value):wired.add(key.value)
    for key in providers:
        if not re.fullmatch('[a-z][a-z0-9_]*',key):raise DocsError('Unsupported provider identifier in public catalog.')
        tree=ast.parse(source.text('src/aiusage/'+key+'.py'))
        if not any(isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name=='collect' for node in tree.body):
            raise DocsError('Listed provider has no built-in collector: '+key)
        if key not in wired:raise DocsError('Listed provider is not connected to collection: '+key)
    native=source.text('native/MenuBar.swift')
    floating=bool(re.search(r'\bclass\s+FloatingWidget\b',native) and 'NSPanel' in native)
    installer=ast.parse(source.text('scripts/install_menubar.py'))
    minimum='13.0'
    for node in ast.walk(installer):
        if isinstance(node,ast.Dict):
            for key,value in zip(node.keys,node.values):
                if isinstance(key,ast.Constant) and key.value=='LSMinimumSystemVersion' and isinstance(value,ast.Constant):minimum=str(value.value)
    paths=[p for p in source.modes if p.startswith(SOURCE_ROOTS) or p in ('pyproject.toml','Makefile','LICENSE')]
    return project,registry,providers,floating,minimum.removesuffix('.0'),source.digest(paths)


def contents(source):
    project,registry,providers,floating,macos,digest=facts(source)
    python=project['requires-python'];minimum=re.search(r'>=\s*([0-9.]+)',python)
    minimum=minimum[1] if minimum else python
    license_title=source.text('LICENSE').splitlines()[0]
    license_name='MIT' if license_title=='MIT License' else license_title
    names=[p['name'] for p in providers.values()]
    joined=', '.join(names[:-1])+', and '+names[-1] if len(names)>1 else names[0]
    native_note=('The floating widget appears on the first launch of the native companion. Drag it to your preferred position; its position and visibility preference are remembered. Use the Headroom menu to hide or show it. Collection continues while the widget is hidden.' if floating else
                 'The native companion shows current quota in the menu bar and opens the local dashboard.')
    features=[
        f'- **{len(providers)} plans at a glance:** remaining allowance, provider-reported tier, connection state, and the reset for the limiting window.',
        '- **Observed burn and safe pace:** percentage points consumed per hour, conditional runout estimates, and the pace that would preserve quota until reset.',
        '- **Quota history:** explore each provider and allowance over time; export stored snapshots as CSV with account identities omitted.',
        '- **Local model accounting:** per-model receipts, known token counts, requests, and rolling token velocity, kept separate from subscription quota.',
        '- **Freshness you can see:** expired readings and connection failures withdraw current percentages and forecasts while keeping labelled history.',
        '- **Native menu indicator:** a macOS companion showing current remaining quota and opening the dashboard.']
    if floating:features.append('- **Floating desktop widget:** a draggable quota panel that stays visible across desktops. Hide or show it from the menu bar, and open the dashboard from the widget.')
    features.append('- **Local storage:** SQLite history, private file permissions, and an HTTP service bound to `127.0.0.1`.')
    requirements=[f'- **macOS {macos}+** for the supported provider integrations and native companion.',f'- **Python {minimum}+** (`{python}`). '+('The Python runtime uses the standard library; there are no third-party runtime dependencies.' if not project.get('dependencies') else 'Runtime packages: '+', '.join(project['dependencies'])+'.'),
        '- **Node.js** to run the local JavaScript checks.',
        '- **Codex, Cursor, and Antigravity**, signed in through their official clients, for the providers you use. Headroom includes its provider collectors; no CodexBar installation is needed.',
        '- **Muse Code** and **Claude Code**, signed in through their official clients, for their direct OAuth collectors.',
        '- **Xcode command-line tools** if you want the native companion (`xcode-select --install`).']
    rows=['| Provider | Built-in collector | Account / quota portal |','| --- | --- | --- |']
    for key,item in providers.items():
        name=str(item['name']).replace('|','\\|');url=item['connection_url']
        if not isinstance(url,str) or not url.startswith('https://') or any(c in url for c in '\r\n<>'):raise DocsError('Invalid public provider portal.')
        rows.append(f'| **{name}** | [{key}.py](src/aiusage/{key}.py) | [Open provider portal]({url}) |')
    return {
        'badges':f'[![Python {minimum}+](https://img.shields.io/badge/Python-{minimum}%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)\n[![macOS {macos}+](https://img.shields.io/badge/macOS-{macos}%2B-222222?logo=apple)](#requirements)\n[![{license_name} license](https://img.shields.io/badge/License-{license_name.replace(" ","%20")}-397A61)](LICENSE)',
        'overview':f'Headroom brings **{joined}** into one view. It reads existing authenticated provider connections, keeps quota history on your machine, and shows reset-aware estimates when the observations support them. '+('A native macOS menu companion and floating desktop widget keep current capacity within reach.' if floating else 'A native macOS menu companion keeps current capacity within reach.'),
        'snapshot':f'**Current source:** version `{project["version"]}` · {len(providers)} built-in provider collectors · readings expire after {registry["MAX_DATA_AGE_SECONDS"]} seconds · default polling {registry["DEFAULT_SETTINGS"]["refresh_interval_seconds"]} seconds.\n\n<sub>These facts are refreshed from the committed source. Source fingerprint: `{digest[:16]}`.</sub>',
        'features':'\n'.join(features),'requirements':'\n'.join(requirements),'providers':'\n'.join(rows),'native-note':native_note}


def render(source):
    text=source.text('README.md');values=contents(source)
    for key in BLOCKS:
        start=f'<!-- headroom:{key}:start -->';end=f'<!-- headroom:{key}:end -->'
        if text.count(start)!=1 or text.count(end)!=1:raise DocsError('README sync markers are missing or duplicated: '+key)
        pattern=re.escape(start)+r'.*?'+re.escape(end)
        text=re.sub(pattern,lambda _:start+'\n'+values[key]+'\n'+end,text,flags=re.S)
    return text


def preview_check(source):
    try:receipt=json.loads(source.text(PREVIEW))
    except (ValueError,KeyError):raise DocsError('Synthetic dashboard preview receipt is invalid.') from None
    paths=[p for p in source.modes if p.startswith('web/')]
    image='docs/assets/dashboard.jpg'
    if receipt.get('image_sha256')!=hashlib.sha256(source.read(image)).hexdigest() or receipt.get('frontend_sha256')!=source.digest(paths):
        raise DocsError('Dashboard preview is stale. Refresh the synthetic screenshot, run make preview-receipt, stage the image/receipt, and recommit.')


def check(source):
    expected=render(source)
    if source.text('README.md')!=expected:raise DocsError('README facts are stale for this source revision. Run make docs, stage README.md, and recommit.')
    native=source.text('native/MenuBar.swift')
    if '--self-test' in source.text('scripts/install_menubar.py') and '--self-test' not in native:
        raise DocsError('Native installer requires a self-test flag that the companion does not implement.')
    if 'docs/operations.md' in source.modes:
        if '## Floating desktop widget' in source.text('docs/operations.md') and not facts(source)[3]:
            raise DocsError('Floating widget documentation does not match the native implementation.')
    for target in re.findall(r'\]\(([^)]+)\)',expected):
        if target.startswith(('http://','https://','#','mailto:')):continue
        path=target.split('#')[0]
        if path not in source.modes:raise DocsError('README points to a missing public file: '+path)
        if source.modes[path]=='120000':raise DocsError('README assets must not be symbolic links: '+path)
    workflows=[p for p in source.modes if p.startswith('.github/workflows/') and p.endswith(('.yml','.yaml'))]
    if workflows:raise DocsError('Hosted workflow review is required before publication; push/PR/schedule CI is prohibited by AGENTS.md.')
    preview_check(source)


def prepare(root):
    source=Source(root,'INDEX');old=source.text('README.md');new=render(source)
    if old==new:return
    readme=Path(root)/'README.md';working=readme.read_text()
    current=git(root,'rev-parse',':README.md').decode().strip()
    if current!=source.objects['README.md']:raise DocsError('Staged README changed during refresh; retry the commit without concurrent index edits.')
    blob=git(root,'hash-object','-w','--stdin',input=new.encode()).decode().strip()
    git(root,'update-index','--cacheinfo',source.modes['README.md'],blob,'README.md')
    preserved=working!=old or readme.read_text()!=old
    if not preserved:readme.write_text(new)
    print('Refreshed README facts from staged source'+('; preserved unstaged README edits.' if preserved else '.'))


def pre_push(root,lines):
    commits=set()
    for line in lines.splitlines():
        fields=line.split()
        if len(fields)!=4:raise DocsError('Invalid pre-push ref input.')
        _,local_oid,_,_=fields
        if ZERO.fullmatch(local_oid):continue
        if not OID.fullmatch(local_oid):raise DocsError('Invalid pushed object identifier.')
        commit=git(root,'rev-parse','--verify',local_oid+'^{commit}').decode().strip()
        commits.add(commit)
    for commit in sorted(commits):check(Source(root,commit))
    if commits:print(f'Public README and preview verified for {len(commits)} pushed source revision(s).')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo',type=Path,default=Path(__file__).resolve().parents[1])
    modes=parser.add_mutually_exclusive_group(required=True)
    for option in ('write','check','prepare-commit','pre-push','record-preview'):modes.add_argument('--'+option,action='store_true')
    parser.add_argument('--revision',help='Check a committed revision instead of working files')
    args=parser.parse_args()
    try:
        if args.prepare_commit:prepare(args.repo)
        elif args.pre_push:pre_push(args.repo,sys.stdin.read())
        elif args.record_preview:
            source=Source(args.repo);paths=[p for p in source.modes if p.startswith('web/')]
            data={'image':'dashboard.jpg','image_sha256':hashlib.sha256(source.read('docs/assets/dashboard.jpg')).hexdigest(),'frontend_sha256':source.digest(paths)}
            (args.repo/PREVIEW).write_text(json.dumps(data,indent=2)+'\n');print('Recorded the synthetic preview source receipt.')
        else:
            source=Source(args.repo,args.revision)
            if args.write:(args.repo/'README.md').write_text(render(source));print('README facts refreshed from repository source.')
            else:check(source);print('Public README and synthetic preview match source.')
    except (DocsError,OSError,ValueError,KeyError,SyntaxError) as error:
        print('GitHub page check failed: '+str(error),file=sys.stderr);return 1
    return 0


if __name__=='__main__':raise SystemExit(main())
