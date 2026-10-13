"""Concurrent authenticated quota collection with one owner and bounded retries."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from .analytics import iso
from .codexbar import collect
from .claude import collect as collect_claude
from .identity import identity_hasher, configured_credential_scope
from .registry import PROVIDERS


class Collectors:
    def __init__(self, store, muse_collector=None):
        self.store=store
        self.hasher=identity_hasher(store)
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
                result=collect(provider,self.hasher,timeout_seconds=45)
                if provider=='antigravity':
                    grouped=[w for w in result.get('windows',[]) if w.get('label','').startswith(('Gemini ','Claude/GPT '))]
                    if grouped:result['windows']=grouped  # Slot bars are aliases of these explicitly named groups.
                if not result.get('account_ref'):
                    credential=configured_credential_scope(provider)
                    if credential:result['account_ref']=self.store.fingerprint(provider+'|configured|'+credential)
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
