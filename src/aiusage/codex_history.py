"""Import only authenticated Codex quota fields, never conversations or credit balances."""
from pathlib import Path
import json
import hashlib
import os
import sqlite3
from datetime import datetime, timezone
from .analytics import iso, percentage
from .identity import CODEX_HOME, codex_account_id

_UNREAD=object()


def _resume(old,inode,size,prefix):
    offset,state=0,{}
    if old and old['inode']==inode and old['offset']<=size:
        try:previous=json.loads(old['state'])
        except ValueError:previous={}
        if isinstance(previous,dict) and previous.get('_prefix')==prefix:
            offset,state=old['offset'],previous
    state['_prefix']=prefix
    return offset,state


class CodexQuotaHistory:
    MAX_LINE=32*1024*1024
    def __init__(self,store,home=CODEX_HOME): self.store,self.home=store,Path(home)

    def scan_file(self,path,account,checkpoint=_UNREAD):
        stat=path.stat();inode=f'{stat.st_dev}:{stat.st_ino}'
        if checkpoint is _UNREAD:
            with self.store.connect() as db:
                checkpoint=db.execute('SELECT * FROM imports WHERE path=?',(str(path),)).fetchone()
        with path.open('rb') as f:
            # Bind the preflight to the file actually opened, including a
            # replacement between path.stat() and open().
            stat=os.fstat(f.fileno());inode=f'{stat.st_dev}:{stat.st_ino}'
            prefix=hashlib.sha256(f.read(min(stat.st_size,256))).hexdigest()
            offset,state=_resume(checkpoint,inode,stat.st_size,prefix)
            if offset==stat.st_size:return
            read_bytes=0
            with self.store.lock,self.store.connect() as db:
                # Changed files use the latest committed checkpoint under the
                # writer lock; the bulk snapshot is only an unchanged preflight.
                old=db.execute('SELECT * FROM imports WHERE path=?',(str(path),)).fetchone()
                offset,state=_resume(old,inode,stat.st_size,prefix)
                if offset==stat.st_size:return
                f.seek(offset)
                while read_bytes<16*1024*1024:
                    pos=f.tell();line=f.readline(self.MAX_LINE+1)
                    if not line:break
                    if len(line)>self.MAX_LINE:
                        while line and not line.endswith(b'\n'):line=f.readline(self.MAX_LINE+1)
                        offset=f.tell();continue
                    if not line.endswith(b'\n'):break
                    offset=f.tell();read_bytes+=len(line)
                    if not any(t in line[:700] for t in (b'"session_meta"',b'"turn_context"',b'"token_count"')):continue
                    try:e=json.loads(line)
                    except ValueError:continue
                    p=e.get('payload')or{}
                    if not isinstance(p,dict):continue
                    if e.get('type')=='session_meta':
                        # Keep only a HMAC of the source account, not its raw id.
                        owner=p.get('creator_account_id')
                        state['account']=self.store.fingerprint('codex|'+str(owner)) if owner else None
                        state['model']=None
                    elif e.get('type')=='turn_context':state['model']=p.get('model')
                    elif e.get('type')=='event_msg' and p.get('type')=='token_count':
                        q=p.get('rate_limits')or{}
                        if not isinstance(q,dict) or q.get('limit_id')!='codex':continue
                        if state.get('account')!=account:continue
                        model=state.get('model')or''
                        if not model.startswith(('gpt-','codex-')):continue
                        for slot in ('primary','secondary'):
                            raw=q.get(slot)
                            if not isinstance(raw,dict):continue
                            used=percentage(raw.get('used_percent'));mins=raw.get('window_minutes')
                            if used is None or type(mins)is not int:continue
                            if mins==10080:wid,label='secondary-10080m','Weekly'
                            elif mins==300:wid,label='primary-300m','5-hour'
                            else:continue  # Unknown budget semantics cannot be merged into a current pool.
                            reset=raw.get('resets_at')
                            try:reset=iso(datetime.fromtimestamp(reset,timezone.utc)) if type(reset)in(int,float) else None
                            except (ValueError,OverflowError,OSError):reset=None
                            self.store.record_window('codex',account,{'id':wid,'label':label,
                                'used_pct':str(used),'remaining_pct':str(max(0,100-used)),
                                'window_minutes':mins,'resets_at':reset,'observed_at':e.get('timestamp'),
                                'status':'fresh','source':'codex_authenticated_rate_limit_record'},db)
                db.execute('INSERT OR REPLACE INTO imports(path,inode,offset,state) VALUES (?,?,?,?)',(str(path),inode,offset,json.dumps(state)))

    def scan(self):
        account_id=codex_account_id()
        if not account_id:return
        account=self.store.fingerprint('codex|'+account_id)
        # One snapshot of checkpoints prevents hundreds of SQLite opens every
        # five seconds just to rediscover that archived files are unchanged.
        # scan_file still checks inode, size and prefix against each real file.
        try:
            with self.store.connect() as db:
                checkpoints={row['path']:dict(row) for row in db.execute('SELECT * FROM imports')}
        except sqlite3.Error:checkpoints=None
        files=[]
        for path in list((self.home/'sessions').rglob('*.jsonl'))+list((self.home/'archived_sessions').rglob('*.jsonl')):
            try:files.append((path.stat().st_mtime,path))
            except OSError:continue
        for _,path in sorted(files,key=lambda item:item[0],reverse=True):
            try:self.scan_file(path,account,checkpoints.get(str(path)) if checkpoints is not None else _UNREAD)
            except (OSError,ValueError,sqlite3.Error):continue
