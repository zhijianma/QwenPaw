import {browser,setup,settle,out,url,verified,write} from './run-browser.mjs';
import fs from 'node:fs';
import path from 'node:path';
try{for(const port of [5173,5175])for(const size of ['100','1000']){
  const id=`trace-${port}-${size}`;const s=await setup(port,size,'cold',id);const events=[];
  s.cdp.on('Tracing.dataCollected',e=>events.push(...e.value));
  await s.cdp.send('Network.clearBrowserCache');
  await s.cdp.send('Profiler.enable');await s.cdp.send('Profiler.start');
  await s.cdp.send('Tracing.start',{categories:'devtools.timeline,v8,blink.user_timing,disabled-by-default-devtools.timeline',options:'record-as-much-as-possible'});
  let status='complete';try{await s.page.goto(url(port,verified[size].id),{waitUntil:'domcontentloaded',timeout:60000});await settle(s);await s.page.waitForTimeout(2000);}catch(e){status=e.message;}
  const {profile}=await s.cdp.send('Profiler.stop');
  fs.writeFileSync(path.join(out,'traces',id+'.cpuprofile'),JSON.stringify(profile));
  const nodes=new Map(profile.nodes.map(n=>[n.id,n.callFrame]));const sampled=new Map();
  for(let i=0;i<(profile.samples||[]).length;i++){const node=profile.samples[i];sampled.set(node,(sampled.get(node)||0)+(profile.timeDeltas?.[i]||0)/1000);}
  const topFunctions=[...sampled].sort((a,b)=>b[1]-a[1]).slice(0,40).map(([id,ms])=>({ms,...nodes.get(id)}));
  const completion=new Promise(resolve=>s.cdp.once('Tracing.tracingComplete',resolve));await s.cdp.send('Tracing.end');await completion;
  fs.writeFileSync(path.join(out,'traces',id+'.json'),JSON.stringify({traceEvents:events}));
  const complete=events.filter(e=>e.ph==='X'&&typeof e.dur==='number');
  const totals={};for(const e of complete)totals[e.name]=(totals[e.name]||0)+e.dur/1000;
  const sample=await s.page.evaluate(()=>chatPerf.stop());
  write(`traces/${id}-summary.json`,{status,sample,eventCount:events.length,totals,topFunctions,note:'Event categories can nest; CPU profile samples attribute leaf time and are sampled, not exact exclusive wall times.'});
  console.log(JSON.stringify({id,status,eventCount:events.length,top:Object.entries(totals).sort((a,b)=>b[1]-a[1]).slice(0,8)}));await s.context.close();
}}finally{await browser.close();}
