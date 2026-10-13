"""Concurrent authenticated quota collection with one owner and bounded retries."""
import threading
import time
from .analytics import iso,number
from .codex import collect as collect_codex
from .cursor import collect as collect_cursor
from .zai import collect as collect_zai
from .antigravity import collect as collect_antigravity
from .claude import collect as collect_claude
from .registry import PROVIDERS,DEFAULT_SETTINGS
from .collector_process import ProviderProcess


class Collectors:
    def __init__(self, store, muse_collector=None,isolated=True,deadline=45):
        self.store=store
        self.muse_collector=muse_collector
        self.stop=threading.Event()
        self.wakeup=threading.Event()
        self.lock=threading.Lock()
        self.busy=set()
        self.retry_delay={provider:0 for provider in PROVIDERS}
        self.next_allowed={provider:0 for provider in PROVIDERS}
        self.events={provider:threading.Event() for provider in PROVIDERS}
        self.workers={provider:ProviderProcess(provider,store.root,deadline) for provider in PROVIDERS} if isolated else {}
        self.refresh_requested=set();self.threads=[]

    @property
    def synchronizing(self):
        with self.lock:return bool(self.busy)

    def request_refresh(self):
        with self.lock:self.refresh_requested.update(PROVIDERS)
        for event in self.events.values():event.set()

    def fetch(self,provider):
        if self.stop.is_set():return
        try:
            if provider in self.workers:
                with self.lock:
                    refresh=provider in self.refresh_requested;self.refresh_requested.discard(provider)
                result=self.workers[provider].fetch(refresh)
            elif provider=='muse':
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
            if result.get('error') and result['error'].get('code') in ('rate_limited','collection_timeout'):
                supplied=number(result.get('retry_after_seconds'))
                supplied=float(max(60,min(3600,supplied))) if supplied is not None else 60
                self.retry_delay[provider]=max(supplied,min(900,self.retry_delay[provider]*2))
                self.next_allowed[provider]=time.monotonic()+self.retry_delay[provider]
            else:self.retry_delay[provider]=0;self.next_allowed[provider]=0
            if not self.stop.is_set():self.store.save_provider(result)
        except Exception:
            if self.stop.is_set():return
            self.store.save_provider({'id':provider,'status':'error','auth_verified':False,
                'auth_method':'unknown','observed_at':iso(),'windows':[],'source':'collector',
                'error':{'code':'collector_error','message':'Provider collection failed; credentials and raw responses were not logged.'}})

    def refresh(self):
        self.request_refresh()

    def run(self,provider):
        event=self.events[provider]
        while not self.stop.is_set():
            delay=self.next_allowed[provider]-time.monotonic()
            if delay>0:
                self.stop.wait(min(delay,1));continue
            event.clear();started=time.monotonic()
            with self.lock:self.busy.add(provider)
            try:self.fetch(provider)
            except Exception:pass
            finally:
                with self.lock:self.busy.discard(provider)
            try:interval=self.store.settings()['refresh_interval_seconds']
            except Exception:interval=DEFAULT_SETTINGS['refresh_interval_seconds']
            interval=max(interval,self.retry_delay[provider])
            # Each provider owns its cadence; one slow/auth-blocked service never stalls the others.
            event.wait(max(5,interval-(time.monotonic()-started)))

    def start(self):
        for provider in PROVIDERS:
            thread=threading.Thread(target=self.run,args=(provider,),daemon=True);self.threads.append(thread);thread.start()

    def close(self,timeout=5):
        self.stop.set();self.request_refresh()
        until=time.monotonic()+timeout
        for worker in self.workers.values():worker.close(timeout=max(0,until-time.monotonic()))
        for thread in self.threads:thread.join(timeout=max(0,until-time.monotonic()))
