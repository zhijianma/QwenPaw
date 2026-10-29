import {browser,setup,settle,config,out,url,verified,write} from './run-browser.mjs';
const results=[];
try{for(const port of [5173,5175]){
  const s=await setup(port,'50','cold',`negative-${port}`);
  await s.page.goto(url(port,verified['50'].id));await settle(s);
  const baseline=await s.page.evaluate(()=>{chatPerf.stop();return [...document.querySelectorAll('[data-role][id]')].map(e=>e.id)});
  const wrong=config(port,'100','first-switch',`negative-${port}-residual`);
  // Keep the real route but resolve IDs from an unvisited target session.
  wrong.path='/chat/'+verified['50'].id;
  await s.page.evaluate(c=>{c.resolveTargets=(els,cfg)=>{const copy={...cfg,path:'/chat/6f0cb439-2b27-467d-84b1-119a74b05641'};resolvePerfTargets(els,copy);cfg.expectedMessageIds=copy.expectedMessageIds;cfg.latestMessageId=copy.latestMessageId;};chatPerf.start(c);},wrong);
  await s.page.waitForTimeout(700);const residual=await s.page.evaluate(()=>chatPerf.stop('negative-control'));
  const missing=config(port,'50','first-switch',`negative-${port}-missing-latest`);
  await s.page.evaluate(c=>{c.expectedMessageIds=[...document.querySelectorAll('[data-role][id]')].map(e=>e.id);c.latestMessageId='verified-message-not-loaded';chatPerf.start(c)},missing);
  await s.page.waitForTimeout(700);const partial=await s.page.evaluate(()=>chatPerf.stop('negative-control'));
  results.push({port,baseline,residual,partial,passed:residual.firstVisibleMs===null&&residual.latestVisibleMs===null&&partial.firstStableMs===null});write('calibration/negative-controls.json',results);console.log(port,results.at(-1).passed);await s.context.close();
}}finally{await browser.close();}
