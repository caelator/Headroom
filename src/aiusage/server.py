"""Local, read-only plan monitoring API and browser dashboard."""
import argparse
import csv
from datetime import timedelta
import io
import json
from pathlib import Path
import signal
import threading
import tempfile
import urllib.request
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import parse_qs,urlsplit
from .analytics import iso,utc_now,timestamp
from .collectors import Collectors
from .codex_history import CodexQuotaHistory
from .registry import PROVIDERS,DATA_DIR
from .store import Store
from .local_models import Store as ModelStore, Importer as ModelImporter, HOME as MODEL_HOME

ROOT=Path(__file__).resolve().parents[2]


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
    try:
        from .muse import collect as muse_collect
    except ImportError:muse_collect=None
    collectors=Collectors(store,muse_collect)
    history=CodexQuotaHistory(store)
    models=ModelStore()
    model_importer=ModelImporter(models)
    stop=threading.Event()
    collectors.start()
    def import_history():
        while not stop.is_set():
            try:history.scan()
            except Exception:pass  # Never print history/auth content.
            stop.wait(5)
    threading.Thread(target=import_history,daemon=True).start()
    def import_models():
        while not stop.is_set():
            try:
                model_importer.scan(); models.set_meta('collector_error',False)
            except Exception:models.set_meta('collector_error',True)
            stop.wait(5)
    threading.Thread(target=import_models,daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass

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
            except (BrokenPipeError,ConnectionResetError):pass

        def do_GET(self):
            if not self.local():self.send(403,{'error':'Local access only'});return
            url=urlsplit(self.path);query=parse_qs(url.query);path=url.path
            assets={'/':('index.html','text/html; charset=utf-8'),'/app.js':('app.js','text/javascript; charset=utf-8'),
                    '/styles.css':('styles.css','text/css; charset=utf-8')}
            if path in assets:
                file,kind=assets[path]
                try:self.send(200,(ROOT/'web'/file).read_bytes(),kind)
                except OSError:self.send(503,{'error':'Frontend installation incomplete'})
                return
            try:
                if path in ('/api/plans','/health'):
                    self.send(200,store.portfolio(collectors.synchronizing))
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
                    with models.connect() as db:
                        for minutes in (15,60,360):
                            cutoff=iso(utc_now()-timedelta(minutes=minutes))
                            row=db.execute('''SELECT coalesce(sum(input_tokens),0),coalesce(sum(output_tokens),0),
                                coalesce(sum(total_tokens),0),count(*),sum(total_tokens IS NULL)
                                FROM events WHERE timestamp>=?''',(cutoff,)).fetchone()
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
                        with store.connect() as db:
                            for record in db.execute('SELECT '+','.join(keys)+' FROM observations ORDER BY observed_at DESC'):
                                row=dict(record)
                                for key,value in row.items():
                                    if isinstance(value,str) and value.startswith(('=','+','-','@','\t','\r')):row[key]="'"+value
                                writer.writerow(row)
                        wrapper.flush();output.seek(0)
                        self.send(200,output,'text/csv; charset=utf-8','headroom-quota-history.csv')
                        wrapper.detach()
                else:self.send(404,{'error':'Not found'})
            except ValueError as e:self.send(400,{'error':str(e)})
            except Exception:self.send(503,{'error':'Plan data is temporarily unavailable.'})

        def do_POST(self):
            if not self.local():self.send(403,{'error':'Local access only'});return
            path=urlsplit(self.path).path
            if self.headers.get('X-AIUsage-Action')!='local':
                self.send(403,{'error':'Local action header required'});return
            if self.headers.get('Content-Type','').split(';')[0]!='application/json':
                self.send(415,{'error':'JSON required'});return
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=1024*1024:raise ValueError('Request size limit exceeded')
                body=json.loads(self.rfile.read(length))
                if not isinstance(body,dict):raise ValueError('JSON object required')
                if path=='/api/refresh':
                    from .muse import retry_credentials
                    retry_credentials();collectors.request_refresh();self.send(202,{'synchronizing':True})
                elif path=='/api/settings':
                    settings=store.save_settings(body);collectors.request_refresh();self.send(200,{'settings':settings})
                else:self.send(404,{'error':'Not found'})
            except (ValueError,TypeError) as e:self.send(400,{'error':str(e)})
            except Exception:self.send(503,{'error':'Action could not be completed.'})

    server=ThreadingHTTPServer(('127.0.0.1',port),Handler);server.daemon_threads=True
    def terminate(*args):
        stop.set();collectors.stop.set();collectors.request_refresh()
        threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,terminate);signal.signal(signal.SIGINT,terminate)
    try:server.serve_forever()
    finally:stop.set();server.server_close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8793)
    parser.add_argument('--data-dir',type=Path,default=DATA_DIR)
    parser.add_argument('--snapshot',action='store_true')
    options=parser.parse_args();store=Store(options.data_dir)
    if options.snapshot:print(json.dumps(store.portfolio(),indent=2))
    else:serve(store,options.port)


if __name__=='__main__':main()
