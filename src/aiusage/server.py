"""Local, read-only plan monitoring API and browser dashboard."""
import argparse
import csv
from datetime import timedelta
import io
import json
import os
from pathlib import Path
import signal
import socket
import threading
import tempfile
import urllib.request
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import parse_qs,urlsplit
from .analytics import iso,utc_now,timestamp
from .collectors import Collectors
from .codex_history import CodexQuotaHistory
from .registry import PROVIDERS,DATA_DIR
from .store import Store,StorageError
from .local_models import Store as ModelStore, Importer as ModelImporter, HOME as MODEL_HOME
from .release import identity,API_VERSION

ROOT=Path(__file__).resolve().parents[2]
MAX_HTTP_CONNECTIONS=24
HTTP_TIMEOUT_SECONDS=5


class LocalHTTPServer(ThreadingHTTPServer):
    """Bound connection count and idle reads/writes, including incomplete POSTs."""
    daemon_threads=True
    block_on_close=False
    request_queue_size=32

    def __init__(self,*args,**kwargs):
        self.slots=threading.BoundedSemaphore(MAX_HTTP_CONNECTIONS)
        super().__init__(*args,**kwargs)

    def process_request(self,request,address):
        request.settimeout(HTTP_TIMEOUT_SECONDS)
        if not self.slots.acquire(blocking=False):
            try:request.sendall(b'HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            except OSError:pass
            self.shutdown_request(request);return
        try:super().process_request(request,address)
        except BaseException:
            self.slots.release();self.shutdown_request(request);raise

    def process_request_thread(self,request,address):
        try:super().process_request_thread(request,address)
        finally:self.slots.release()

    def handle_error(self,*args):pass


def model_catalog():
    items={}
    for name in ('meta-models.json','models_cache.json'):
        try:
            for model in json.loads((MODEL_HOME/name).read_text()).get('models',[]):
                if isinstance(model,dict) and (model.get('slug') or model.get('id')):
                    items[model.get('slug') or model['id']]=model
        except (OSError,ValueError,AttributeError):pass
    return list(items.values())


def serve(store,port=8793):
    release=identity(ROOT)
    service={'name':'headroom','api_version':API_VERSION,'version':release['version'],
             'source_fingerprint':release['source_fingerprint'],'pid':os.getpid(),'started_at':iso()}
    try:
        from .muse import collect as muse_collect
    except ImportError:muse_collect=None
    collectors=Collectors(store,muse_collect)
    stop=threading.Event()
    def import_history():
        while not stop.is_set():
            try:history.scan()
            except Exception:pass  # Never print history/auth content.
            stop.wait(5)
    def import_models():
        while not stop.is_set():
            try:
                model_importer.scan(); models.set_meta('collector_error',False)
            except Exception:models.set_meta('collector_error',True)
            stop.wait(5)

    def maintain_history():
        while not stop.is_set():
            try:store.maintenance()
            except Exception:pass  # Failed archiving keeps active rows for retry.
            stop.wait(3600)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass

        def setup(self):
            super().setup();self.read_expired=False
            def expire():
                self.read_expired=True
                try:self.connection.shutdown(socket.SHUT_RD)
                except OSError:pass
            self.read_timer=threading.Timer(HTTP_TIMEOUT_SECONDS,expire)
            self.read_timer.daemon=True;self.read_timer.start()

        def finish(self):
            self.read_timer.cancel();super().finish()

        def local(self):
            allowed={f'127.0.0.1:{port}',f'localhost:{port}'}
            origin=self.headers.get('Origin')
            valid_origin=not origin or origin in {'http://'+h for h in allowed}
            return self.headers.get('Host') in allowed and valid_origin

        def send(self,code,data,kind='application/json',attachment=None):
            if isinstance(data,(dict,list)):data=json.dumps(data,allow_nan=False).encode()
            elif isinstance(data,str):data=data.encode()
            stream = hasattr(data,'read') and hasattr(data,'seek')
            if stream:
                data.seek(0,2); length=data.tell(); data.seek(0)
            else:length=len(data)
            try:
                self.send_response(code)
                self.send_header('Content-Type',kind)
                self.send_header('Content-Length',str(length))
                self.send_header('Cache-Control','no-store')
                self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Referrer-Policy','no-referrer')
                self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'")
                if attachment:self.send_header('Content-Disposition','attachment; filename="'+attachment+'"')
                self.end_headers()
                if stream:
                    for chunk in iter(lambda:data.read(65536),b''):self.wfile.write(chunk)
                else:self.wfile.write(data)
            except OSError:pass

        def do_GET(self):
            self.read_timer.cancel()
            if not self.local():self.send(403,{'error':'Local access only'});return
            url=urlsplit(self.path);query=parse_qs(url.query);path=url.path
            assets={'/':('index.html','text/html; charset=utf-8'),'/app.js':('app.js','text/javascript; charset=utf-8'),
                    '/styles.css':('styles.css','text/css; charset=utf-8')}
            if path in assets:
                file,kind=assets[path]
                try:
                    content=(ROOT/'web'/file).read_bytes()
                    if path=='/':
                        marker=('<meta name="headroom-source" content="'+service['source_fingerprint']+'">').encode()
                        content=content.replace(b'</head>',marker+b'</head>')
                    self.send(200,content,kind)
                except OSError:self.send(503,{'error':'Frontend installation incomplete'})
                return
            try:
                if path in ('/api/plans','/health'):
                    self.send(200,{**store.portfolio(collectors.synchronizing),'service':service})
                elif path=='/api/history':
                    provider=query.get('provider',['codex'])[0];hours=int(query.get('hours',['24'])[0])
                    if not 1<=hours<=24*90:raise ValueError('History range must be 1–2160 hours')
                    self.send(200,store.history(provider,hours))
                elif path=='/api/local-usage':
                    days=int(query.get('days',['1'])[0])
                    if days not in (0,1,7,30):raise ValueError('Invalid range')
                    report=models.summary(model_catalog(),days)
                    report['import']=report.pop('import_')
                    rates=[]
                    current=iso()
                    with models.connect() as db:
                        for minutes in (15,60,360):
                            cutoff=iso(utc_now()-timedelta(minutes=minutes))
                            row=db.execute('''SELECT coalesce(sum(input_tokens),0),coalesce(sum(output_tokens),0),
                                coalesce(sum(total_tokens),0),count(*),sum(total_tokens IS NULL)
                                FROM events WHERE julianday(timestamp)>=julianday(?)
                                AND julianday(timestamp)<=julianday(?)''',(cutoff,current)).fetchone()
                            rates.append({'window_minutes':minutes,'input_tokens_per_minute':row[0]/minutes,
                                'output_tokens_per_minute':row[1]/minutes,'total_tokens_per_minute':row[2]/minutes,
                                'requests_per_hour':row[3]*60/minutes,'unknown_usage':row[4] or 0})
                    report['velocity']=rates
                    self.send(200,report)
                elif path=='/api/export.csv':
                    keys=['provider_id','window_id','label','observed_at','resets_at','used_pct','remaining_pct','status','source']
                    with tempfile.SpooledTemporaryFile(max_size=4*1024*1024,mode='w+b') as output:
                        wrapper=io.TextIOWrapper(output,encoding='utf-8',newline='')
                        writer=csv.DictWriter(wrapper,fieldnames=keys);writer.writeheader()
                        from .quota_archive import export_rows
                        for record in export_rows(store):
                            row={key:record[key] for key in keys}
                            for key,value in row.items():
                                if isinstance(value,str) and value.startswith(('=','+','-','@','\t','\r')):row[key]="'"+value
                            writer.writerow(row)
                        wrapper.flush();output.seek(0)
                        self.send(200,output,'text/csv; charset=utf-8','headroom-quota-history.csv')
                        wrapper.detach()
                else:self.send(404,{'error':'Not found'})
            except ValueError:self.send(400,{'error':'Invalid request parameters.'})
            except Exception:self.send(503,{'error':'Plan data is temporarily unavailable.'})

        def do_POST(self):
            if not self.local():self.send(403,{'error':'Local access only'});return
            path=urlsplit(self.path).path
            if self.headers.get('X-AIUsage-Action')!='local':
                self.send(403,{'error':'Local action header required'});return
            if self.headers.get('Content-Type','').split(';')[0]!='application/json':
                self.send(415,{'error':'JSON required'});return
            try:
                if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length',[]))!=1:
                    raise ValueError('Explicit single request length required')
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=1024*1024:raise ValueError('Request size limit exceeded')
                raw=self.rfile.read(length)
                if self.read_expired:raise TimeoutError('Request read deadline')
                if len(raw)!=length:raise ValueError('Incomplete request body')
                self.read_timer.cancel()
                body=json.loads(raw)
                if not isinstance(body,dict):raise ValueError('JSON object required')
                if path=='/api/refresh':
                    from .muse import retry_credentials
                    from .claude import retry_credentials as retry_claude
                    retry_credentials();retry_claude();collectors.request_refresh();self.send(202,{'synchronizing':True})
                elif path=='/api/settings':
                    settings=store.save_settings(body);collectors.request_refresh();self.send(200,{'settings':settings})
                else:self.send(404,{'error':'Not found'})
            except (ValueError,TypeError):self.send(400,{'error':'Invalid local request.'})
            except TimeoutError:self.send(408,{'error':'Request deadline exceeded.'})
            except Exception:self.send(503,{'error':'Action could not be completed.'})

    server=LocalHTTPServer(('127.0.0.1',port),Handler)
    import_threads=[]
    previous_signals={}
    try:
        port=server.server_address[1]
        history=CodexQuotaHistory(store)
        models=ModelStore()
        model_importer=ModelImporter(models)
        def terminate(*args):
            stop.set();collectors.stop.set();collectors.request_refresh()
            threading.Thread(target=server.shutdown,daemon=True).start()
        for sig in (signal.SIGTERM,signal.SIGINT):
            previous_signals[sig]=signal.signal(sig,terminate)
        collectors.start()
        import_threads=[threading.Thread(target=import_history,daemon=True),threading.Thread(target=import_models,daemon=True),threading.Thread(target=maintain_history,daemon=True)]
        for thread in import_threads:thread.start()
        server.serve_forever()
    finally:
        stop.set()
        if hasattr(collectors,'close'):collectors.close(timeout=5)
        else:
            collectors.stop.set();collectors.request_refresh()
        for thread in import_threads:
            if thread.ident is not None:thread.join(timeout=1)
        server.server_close()
        for sig,handler in previous_signals.items():signal.signal(sig,handler)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8793)
    parser.add_argument('--data-dir',type=Path,default=DATA_DIR)
    parser.add_argument('--snapshot',action='store_true')
    options=parser.parse_args();store=Store(options.data_dir)
    if options.snapshot:print(json.dumps(store.portfolio(),indent=2))
    else:serve(store,options.port)


if __name__=='__main__':
    try:main()
    except (OSError,ValueError,StorageError):
        raise SystemExit('Headroom could not start. Check the local port, runtime and private storage with scripts/diagnostics.py; preserve existing history and restore a verified backup if storage is damaged.') from None
