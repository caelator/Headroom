"""Observe wire usage without retaining prompts, response bodies, or auth headers."""
import json
import os
import sys
import threading
import time
import uuid
import zlib
from .local_models import Store, clean_text, normalize_usage

_store = None
_lock = threading.Lock()


def store():
    global _store
    with _lock:
        if _store is None:
            _store = Store()
        return _store


def report_failure():
    # Never include exception text: HTTP/provider exceptions can embed payloads.
    sys.stderr.write('usage accounting write failed; inspect local storage health\n')


def initialize():
    try:
        s = store()
        with s.lock, s.connect() as db:
            db.execute("UPDATE events SET status='interrupted' WHERE router=1 AND status='in_progress'")
        def heartbeat():
            while True:
                try:
                    s.set_meta('router_heartbeat', dict(time=time.time(), pid=os.getpid()))
                except Exception:
                    report_failure()
                time.sleep(20)
        threading.Thread(target=heartbeat, daemon=True).start()
    except Exception:
        report_failure()


class Observer:
    MAX_BUFFER = 8 * 1024 * 1024

    def __init__(self, model, headers=None, persist=True):
        self.request_id = uuid.uuid4().hex
        self.model = clean_text(model, 'unknown')
        self.started = time.monotonic()
        self.persist = persist
        self.data = dict(model=self.model, requested_model=self.model, attempts=0,
                         status='in_progress')
        self.content_type = ''
        self.buffer = b''
        self.lines = []
        self.line_bytes = 0
        self.skipping = False
        self.decoder = None
        self.finished = False
        self.terminal_seen = False
        headers = headers or {}
        lower = {k.lower(): v for k, v in headers.items()}
        if persist:
            try:
                store().start_request(self.request_id, self.model,
                                      lower.get('session_id') or lower.get('x-codex-thread-id'),
                                      lower.get('x-codex-turn-id'))
            except Exception:
                report_failure()

    def response(self, status, content_type='', encoding=''):
        self.data['http_status'] = status
        self.content_type = content_type.lower()
        if status >= 400:
            self.data['status'] = 'failed'
        if encoding.lower() == 'gzip':
            self.decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif encoding.lower() == 'deflate':
            self.decoder = zlib.decompressobj()

    def _event(self, raw):
        if not raw or raw == b'[DONE]':
            return
        try:
            event = json.loads(raw)
        except (ValueError, UnicodeError):
            return
        if not isinstance(event, dict):
            return
        response = event.get('response')
        typ = event.get('type', '')
        if 'text/event-stream' in self.content_type and typ not in (
                'response.created', 'response.completed', 'response.failed', 'response.incomplete', 'error'):
            return
        if self.terminal_seen:
            return
        if not isinstance(response, dict):
            response = event
        rid = clean_text(response.get('id'))
        if rid:
            self.data['response_id'] = rid
        model = clean_text(response.get('model'))
        if model:
            self.data['model'] = model
        if isinstance(response.get('usage'), dict):
            self.data.update(normalize_usage(response['usage']))
        statuses = {'response.completed': 'completed', 'response.failed': 'failed',
                    'response.incomplete': 'incomplete', 'error': 'failed'}
        if typ in statuses:
            self.data['status'] = statuses[typ]
            self.terminal_seen = True
        elif response.get('status') in ('completed', 'failed', 'incomplete'):
            self.data['status'] = response['status']
        elif 'text/event-stream' not in self.content_type and response.get('usage') is not None:
            self.data['status'] = 'completed'

    def feed(self, chunk):
        try:
            if self.decoder:
                chunk = self.decoder.decompress(chunk, self.MAX_BUFFER + 1)
                if len(chunk) > self.MAX_BUFFER or self.decoder.unconsumed_tail:
                    self.decoder = None
                    self.skipping = True
                    self.buffer = b''
                    return
            if 'text/event-stream' not in self.content_type:
                candidate = self.buffer + chunk
                probe = candidate.lstrip().removeprefix(b'\xef\xbb\xbf')
                # ChatGPT's Codex endpoint can return SSE with no Content-Type.
                # Sniff only the wire framing; no body text is retained.
                if probe.startswith((b'event:', b'data:')):
                    self.content_type = 'text/event-stream'
                    chunk, self.buffer = probe, b''
                else:
                    if not self.skipping and len(candidate) <= self.MAX_BUFFER:
                        self.buffer = candidate
                    else:
                        self.skipping = True
                        self.buffer = b''
                    return
            self.buffer += chunk
            while b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                line = line.rstrip(b'\r')
                if not line:
                    if not self.skipping:
                        self._event(b'\n'.join(self.lines))
                    self.lines, self.line_bytes, self.skipping = [], 0, False
                elif line.startswith(b'data:') and not self.skipping:
                    value = line[5:].lstrip(b' ')
                    self.line_bytes += len(value)
                    if self.line_bytes <= self.MAX_BUFFER:
                        self.lines.append(value)
                    else:
                        self.lines, self.skipping = [], True
            if len(self.buffer) > self.MAX_BUFFER:
                self.buffer, self.lines, self.skipping = b'', [], True
        except Exception:
            self.buffer, self.lines, self.skipping = b'', [], True

    def finish(self, status=None):
        if self.finished:
            return
        self.finished = True
        if 'text/event-stream' not in self.content_type and not self.skipping:
            self._event(self.buffer)
        elif self.lines and not self.skipping:
            self._event(b'\n'.join(self.lines))
        if status:
            self.data['status'] = status
        elif self.data['status'] == 'in_progress':
            self.data['status'] = 'interrupted'
        self.data['duration_ms'] = round((time.monotonic() - self.started) * 1000)
        if self.persist:
            try:
                store().finish_request(self.request_id, self.data)
            except Exception:
                report_failure()
        self.buffer, self.lines = b'', []


class NullObserver:
    def __init__(self): self.data = {}
    def response(self, *args): pass
    def feed(self, *args): pass
    def finish(self, *args): pass


def request(model, headers):
    try:
        return Observer(model, headers)
    except Exception:
        report_failure()
        return NullObserver()
