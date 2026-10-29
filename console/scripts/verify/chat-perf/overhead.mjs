import { chromium } from '/Users/mazhijian/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs';
import fs from 'node:fs';
const dir='console/scripts/verify/chat-perf/';
const out='console/scripts/verify/artifacts/chat-perf-2026-10-28/';
const probe=fs.readFileSync(dir+'probe.js','utf8');const adapter=fs.readFileSync(dir+'browser-adapter.js','utf8');
const browser=await chromium.launch({channel:'chrome',headless:true,args:['--enable-precise-memory-info']});
const records=[];
try{for(const port of [5173,5175])for(let repetition=1;repetition<=3;repetition++)for(const enabled of repetition%2?[false,true]:[true,false]){
  const context=await browser.newContext({viewport:{width:1440,height:900},deviceScaleFactor:1,colorScheme:'light'});
  const config={sampleId:`overhead-${port}-${repetition}-${enabled}`,scenario:'cold',path:'/chat/fd6b78ba-474e-422f-a093-8a3bdfc5cd6b',startTime:0,scroller:port===5173?'.qwenpaw-bubble-list-scroll':'[data-history-message-window="true"]',reverseScroll:port===5173,viewportTop:72,viewportBottom:748,expectedMessageIds:[]};
  await context.addInitScript({content:adapter+'\n'+(enabled?probe:'')+`\nwindow.controlConfig=${JSON.stringify(config)};controlConfig.resolveTargets=resolvePerfTargets;${enabled?'chatPerf.start(controlConfig);':''}\nwindow.controlFirst=null;window.controlCosts=[];window.controlTimer=setInterval(()=>{const t=performance.now();const list=document.querySelector(controlConfig.scroller);if(!list)return;const els=[...list.querySelectorAll('[data-role][id]')];resolvePerfTargets(els,controlConfig);const e=els.find(e=>e.id===controlConfig.latestMessageId);if(e&&controlFirst===null){const r=e.getBoundingClientRect();if(r.height>0&&r.bottom>72&&r.top<748)controlFirst=performance.now();}controlCosts.push(performance.now()-t);},100);`});
  const page=await context.newPage();const cdp=await context.newCDPSession(page);await cdp.send('Performance.enable');await cdp.send('Network.enable');await cdp.send('Network.clearBrowserCache');
  await page.goto(`http://localhost:${port}${config.path}?agent=UP3wQD`,{waitUntil:'domcontentloaded'});
  await page.waitForFunction(()=>performance.now()>=10000,{},{timeout:30000,polling:100});
  const data=await page.evaluate(enabled=>{clearInterval(controlTimer);return {firstVisible:controlFirst,controlCosts,probe:enabled?chatPerf.stop():null,visibility:document.visibilityState}},enabled);
  records.push({port,repetition,enabled,...data,metrics:await cdp.send('Performance.getMetrics')});
  fs.writeFileSync(out+'calibration/overhead.json',JSON.stringify(records,null,2));
  console.log(JSON.stringify({port,repetition,enabled,first:data.firstVisible}));await context.close();
}}finally{await browser.close();}
