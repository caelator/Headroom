"""Concurrent authenticated quota collection with one owner and bounded retries."""
import threading
import time
from .analytics import iso
from .codex import collect as collect_codex
from .cursor import collect as collect_cursor
from .zai import collect as collect_zai
from .antigravity import collect as collect_antigravity
from .claude import collect as collect_claude
from .registry import PROVIDERS


class Collectors:
    def __init__(self, store, muse_collector=None):
        self.store=store
        self.muse_collector=muse_collector
        self.stop=threading.Event()
        self.wakeup=threading.Event()
        self.lock=threading.Lock()
        self.busy=set()
        self.retry_delay={provider:0 for provider in PROVIDERS}
        self.events={provider:threading.Event() for provider in PROVIDERS}

    @property
    def synchronizing(self):
        with self.lock:return bool(self.busy)

    def request_refresh(self):
        for event in self.events.values():event.set()

    def fetch(self,provider):
        try:
            if provider=='muse':
                result=self.muse_collector(self.store) if self.muse_collector else {
                    'id':'muse','observed_at':iso(),'status':'auth_required',
                    'source':'meta_oauth_portal','auth_method':'oauth','auth_verified':False,
                    'verified_tier':None,'account_ref':None,'windows':[],
                    'error':{'code':'oauth_portal_required','message':'Complete Meta account verification to connect subscription quotas.'}}
            elif provider=='claude':
                result=collect_claude(self.store)
            else:
                adapter={'codex':collect_codex,'cursor':collect_cursor,
                         'zai':collect_zai,'antigravity':collect_antigravity}[provider]
                result=adapter(self.store)
                if not result.get('account_ref') and result.get('windows'):
                    result['status']='partial'
                    result['error']={'code':'identity_unverified','message':'Quota is reported but account continuity could not be verified; forecasts are unavailable.'}
            if result.get('error') and result['error'].get('code')=='rate_limited':
                self.retry_delay[provider]=max(result.get('retry_after_seconds',60),min(900,self.retry_delay[provider]*2))
            else:self.retry_delay[provider]=0
            self.store.save_provider(result)
        except Exception:
            self.store.save_provider({'id':provider,'status':'error','auth_verified':False,
                'auth_method':'unknown','observed_at':iso(),'windows':[],'source':'collector',
                'error':{'code':'collector_error','message':'Provider collection failed; credentials and raw responses were not logged.'}})

    def refresh(self):
        self.request_refresh()

    def run(self,provider):
        event=self.events[provider]
        while not self.stop.is_set():
            event.clear();started=time.monotonic()
            with self.lock:self.busy.add(provider)
            try:self.fetch(provider)
            finally:
                with self.lock:self.busy.discard(provider)
            interval=max(self.store.settings()['refresh_interval_seconds'],self.retry_delay[provider])
            # Each provider owns its cadence; one slow/auth-blocked service never stalls the others.
            event.wait(max(5,interval-(time.monotonic()-started)))

    def start(self):
        for provider in PROVIDERS:threading.Thread(target=self.run,args=(provider,),daemon=True).start()
