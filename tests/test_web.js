'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
// Execute the actual dashboard functions without starting DOM/network polling.
const source=fs.readFileSync('web/app.js','utf8').replace('  init();','  globalThis.check={state,winState,forecastFor,headlineFor,expiredSig,splitSegments,tierInfo,effStatus,hashView,historyLink,effectiveWindow,freshnessDeadline,validService,ensureHistoryProvider};');
let now=Date.UTC(2026,9,13,12);class Clock extends Date {static now(){return now;}}
const ctx={console,URL,URLSearchParams,Intl,Date:Clock,location:{hash:''},window:{history:{replaceState(_a,_b,hash){ctx.location.hash=hash;}}}};vm.createContext(ctx);vm.runInContext(source,ctx);
const c=ctx.check;
c.state.data={settings:{critical_remaining_pct:10,warn_remaining_pct:25}};
const w={id:'weekly',remaining_pct:10,used_pct:90,status:'fresh',observed_at:new Date(now).toISOString(),fresh_until:new Date(now+1000).toISOString(),resets_at:new Date(now+3600000).toISOString(),velocity:{confidence:'observed',pct_per_hour:10}};
const p={status:'verified',remaining_pct:10,windows:[w],plan:{}};
assert.equal(c.winState(w,p),'fresh');assert.equal(c.forecastFor({w,st:'fresh'}).kind,'observed');
const before=c.expiredSig({providers:[p]});w.fresh_until=new Date(now-1).toISOString();
assert.notEqual(c.expiredSig({providers:[p]}),before);assert.equal(c.winState(w,p),'stale');assert.equal(c.effStatus(p),'stale');
assert.equal(c.forecastFor({w,st:c.winState(w,p)}).kind,'stale');
assert.equal(c.headlineFor(p,{w,st:'stale'},[{w,st:'stale'}]).kind,'stale');
w.fresh_until=new Date(now+1000).toISOString();w.resets_at=new Date(now-1).toISOString();assert.equal(c.winState(w,p),'expired');
w.resets_at=new Date(now+3600000).toISOString();c.state.offline=true;assert.equal(c.winState(w,p),'stale');
assert.notEqual(c.headlineFor(p,{w,st:'stale'},[{w,st:'stale'}]).kind,'value');c.state.offline=false;
assert.equal(c.forecastFor({w:{...w,remaining_pct:0},st:'fresh'}).kind,'exhausted');
assert.equal(c.tierInfo({plan:{declared_tier:'Highest plan (user reported)',verified_tier:'Cursor Pro',verification:'provider'}}).label,'Cursor Pro');
const segments=c.splitSegments([{t:1,v:90,reset:1000},{t:500,v:80,reset:1000},{t:1500,v:100,reset:3000},{t:99999,v:90,reset:3000}],10000);
assert.equal(segments.segs.length,3);
ctx.location.hash='#history?provider=muse&hours=72&metric=used';
assert.equal(c.hashView(),'history');assert.equal(c.state.history.provider,'muse');assert.equal(c.state.history.hours,72);assert.equal(c.state.history.metric,'used');
assert.equal(c.historyLink('muse'),'#history?provider=muse&hours=72&metric=used');
ctx.location.hash='#models?days=30';assert.equal(c.hashView(),'models');assert.equal(c.state.local.days,30);
const fresh={...w,id:'current',remaining_pct:50,fresh_until:new Date(now+60000).toISOString(),resets_at:new Date(now+3600000).toISOString()};
const expired={...fresh,id:'old',remaining_pct:0,resets_at:new Date(now-1).toISOString()};
const mixed={...p,remaining_pct:0,limiting_window_id:'old',windows:[expired,fresh]};
assert.equal(c.effectiveWindow(mixed).w.id,'current');assert.equal(c.headlineFor(mixed,c.effectiveWindow(mixed),[]).text,'50%');
mixed.limiting_window_id='missing';assert.equal(c.effectiveWindow(mixed).w.id,'current');
const stale={...fresh,status:'stale',remaining_pct:null,last_known_remaining_pct:50};
const none={...p,remaining_pct:50,windows:[stale]};assert.equal(c.effStatus(none),'stale');assert.notEqual(c.headlineFor(none,c.effectiveWindow(none),[]).kind,'value');
for (const age of [89000,91000]) {
 const value={...fresh,observed_at:new Date(now-age).toISOString(),fresh_until:new Date(now+86400000).toISOString()};
 assert.equal(c.winState(value,p),age===89000?'fresh':'stale');
}
for (const observed_at of [undefined,'invalid',new Date(now+60000).toISOString()]) assert.equal(c.winState({...fresh,observed_at},p),'stale');
const runout={...fresh,id:'runout',remaining_pct:80,velocity:{confidence:'observed',pct_per_hour:20,exhausts_before_reset:true,eta_exhaustion_at:new Date(now+10000).toISOString()}};
assert.equal(c.effectiveWindow({...p,windows:[fresh,runout]}).w.id,'runout');
const unlimited={...fresh,id:'unlimited',remaining_pct:null,status:'unlimited'};
assert.equal(c.effectiveWindow({...p,windows:[unlimited,fresh]}).w.id,'current');
assert.equal(c.headlineFor({...p,windows:[unlimited]},c.effectiveWindow({...p,windows:[unlimited]}),[]).kind,'unlimited');
assert.equal(c.validService({service:{name:'other',api_version:1}}),false);
assert.equal(c.validService({service:{name:'headroom',api_version:1}}),true);
c.state.data.providers=[{id:'codex',name:'Codex'}];c.state.view='history';c.state.history.provider='unknown';c.ensureHistoryProvider();
assert.equal(c.state.history.provider,'codex');assert.ok(ctx.location.hash.includes('provider=codex'));
const fixture=JSON.parse(fs.readFileSync('tests/native-selection.json','utf8'));
const base=fixture.runner.fixed_now_epoch*1000,defaults=fixture.runner.window_defaults;
const stamp=(seconds)=>new Date(base+seconds*1000).toISOString();
for (const item of fixture.cases) {
  now=base+(item.evaluated_after_seconds??0)*1000;
  c.state.offline=false;c.state.data={settings:{critical_remaining_pct:10,warn_remaining_pct:25},freshness_policy:{max_age_seconds:item.max_age_seconds}};
  const windows=(item.windows??[item.window]).map((value,index)=>{
    const eta=value.eta_in_seconds??value.velocity?.eta_in_seconds;
    return {...value,id:value.id??`w${index}`,label:value.label??value.id??'Window',
      status:value.status??defaults.status,
      observed_at:value.observed_at??stamp(value.observed_offset_seconds??-(value.observed_age_seconds??defaults.observed_age_seconds)),
      fresh_until:stamp(value.fresh_until_in_seconds??defaults.fresh_until_in_seconds),
      resets_at:stamp(value.resets_in_seconds??defaults.resets_in_seconds),
      velocity:{confidence:value.confidence??value.velocity?.confidence??(eta!==undefined?'observed':'insufficient'),
        pct_per_hour:value.pct_per_hour??value.velocity?.pct_per_hour,
        exhausts_before_reset:value.exhausts_before_reset??value.velocity?.exhausts_before_reset??(eta!==undefined?true:null),
        eta_exhaustion_at:eta!==undefined?stamp(eta):value.eta_exhaustion_at??value.velocity?.eta_exhaustion_at}};
  });
  const provider={id:'fixture',status:item.provider_status??'verified',windows,limiting_window_id:item.limiting_window_id};
  const chosen=c.effectiveWindow(provider);
  if ('expected_window_id' in item) assert.equal(chosen?.w.id,item.expected_window_id,item.name);
  if ('expected_state' in item) assert.equal(chosen?.st,item.expected_state,item.name);
  const current=chosen?.st==='fresh'&&typeof chosen.w.remaining_pct==='number'&&chosen.w.remaining_pct>=0&&chosen.w.remaining_pct<=100?chosen.w.remaining_pct:null;
  if ('expected_remaining_pct' in item) assert.equal(current,item.expected_remaining_pct,item.name);
  if ('expected_last_known_pct' in item) {
    const history=chosen&&!['fresh','unlimited'].includes(chosen.st)?chosen.w.last_known_remaining_pct??chosen.w.remaining_pct??null:null;
    assert.equal(history,item.expected_last_known_pct,item.name);
  }
  if (item.historical_forecast===false) assert.notEqual(c.forecastFor(chosen).kind,'observed',item.name);
}
console.log(`Dashboard checks and ${fixture.cases.length} shared native/browser selection cases passed.`);
