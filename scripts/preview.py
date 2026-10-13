"""Temporary loopback preview: synthetic fixtures, no credentials or collectors."""
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit, parse_qs
import json
from aiusage.store import Store
from aiusage.analytics import utc_now, iso
from aiusage.registry import PROVIDERS
from aiusage.release import build_manifest
import os

root = Path(__file__).resolve().parents[1]
fingerprint = build_manifest(root)['source_fingerprint']
service = {'name':'headroom','api_version':1,'source_fingerprint':fingerprint,'version':'0.1.0','pid':os.getpid(),'started_at':iso()}
temporary = TemporaryDirectory(prefix='headroom-public-preview-')
store = Store(temporary.name)
settings = list(zip(PROVIDERS, [66, 43, 82, 31, 58, 74], [3, 6, 2, 4, 5, 2]))
tiers = ['Example plan', 'Example plan', 'Example plan', 'Example plan', 'Example plan', 'Example plan']
now = utc_now()
resets = {pid: iso(now + timedelta(hours=4 + i)) for i, (pid, _, _) in enumerate(settings)}

def seed():
    now = utc_now().replace(microsecond=0)
    now -= timedelta(seconds=now.second%30)
    for index, (pid, remaining, rate) in enumerate(settings):
        for sample in range(61):
            age = (60 - sample) * 30
            used = 100 - remaining - rate * age / 3600
            store.save_provider({'id':pid, 'observed_at':iso(now-timedelta(seconds=age)),
                'source':'synthetic_preview', 'status':'verified', 'verified_tier':tiers[index],
                'auth_method':'demo', 'auth_verified':True, 'account_ref':'synthetic-'+pid,
                'windows':[{'id':'window', 'label':'Session allowance', 'used_pct':used,
                    'remaining_pct':100-used, 'resets_at':resets[pid], 'window_minutes':300,
                    'status':'fresh', 'observed_at':iso(now-timedelta(seconds=age)), 'source':'synthetic_preview'}]})

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        path=urlsplit(self.path).path
        status=200
        if path in ('/','/app.js','/styles.css','/demo.css'):
            kind='text/html' if path=='/' else 'text/javascript' if path=='/app.js' else 'text/css'
            if path=='/demo.css':
                data=b'.demo-label{padding:10px 24px;background:#1a2744;color:#c8d9ff;text-align:center;font:600 13px system-ui;letter-spacing:.06em}'
            else:
                data=(root/'web'/('index.html' if path=='/' else path[1:])).read_bytes()
                if path=='/':
                    data=data.replace(b'</head>', ('<meta name="headroom-source" content="'+fingerprint+'"><link rel="stylesheet" href="/demo.css"></head>').encode())
                    data=data.replace(b'<body>', b'<body><div class="demo-label">DEMO PREVIEW &nbsp;\xe2\x80\xa2&nbsp; SYNTHETIC DATA</div>')
        elif path=='/api/plans':
            seed(); payload=store.portfolio(); payload['service']=service
            for p in payload['providers']: p['plan']['declared_tier']=None
            data=json.dumps(payload).encode();kind='application/json'
        elif path=='/api/local-usage':
            data=json.dumps({'models':[{'model':'example-model','requests':48,'total_tokens':128000}],
                'totals':{'total_tokens':128000,'requests':48},'import':{'last_scan':iso()},'daily':[],
                'velocity':[],'warnings':[]}).encode();kind='application/json'
        elif path=='/api/history':
            pid=parse_qs(urlsplit(self.path).query).get('provider',['codex'])[0]
            data=json.dumps(store.history(pid)).encode();kind='application/json'
        else:
            status=404;data=b'Not found';kind='text/plain'
        self.send_response(status);self.send_header('Content-Type',kind+'; charset=utf-8')
        self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)

print('Synthetic preview on http://127.0.0.1:18793/',flush=True)
try: ThreadingHTTPServer(('127.0.0.1',18793),Handler).serve_forever()
finally: temporary.cleanup()
