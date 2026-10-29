import { chromium } from '/Users/mazhijian/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs';
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
const out=path.resolve('console/scripts/verify/artifacts/chat-perf-2026-10-28');
const source=path.resolve('console/scripts/verify/chat-perf');
const verified=JSON.parse(fs.readFileSync(path.join(out,'calibration/data-consistency.json')));
const aux='97ea3e21-72a9-429d-8494-767a6a0cb8f6';
const write=(name,value)=>fs.writeFileSync(path.join(out,name),JSON.stringify(value,null,2));
const adapter=fs.readFileSync(path.join(source,'browser-adapter.js'),'utf8');
const probe=fs.readFileSync(path.join(source,'probe.js'),'utf8');
const browser=await chromium.launch({channel:'chrome',headless:process.env.CHAT_PERF_HEADFUL!=='1',args:['--enable-precise-memory-info']});
const mode=import.meta.url===pathToFileURL(process.argv[1]).href?(process.argv[2]||'calibration'):'library';
const selectedPort=process.argv[3];
const selectedSize=process.argv[4];
const settings={qwenpaw_assistant_message_display_mode:'result-collapsed',qwenpaw_tool_display_mode:'current',qwenpaw_show_thinking:'true',qwenpaw_chat_wide_mode:'false',language:'zh'};
function config(port,size,scenario,id){return {sampleId:id,scenario,path:'/chat/'+verified[size].id,expectedMessageIds:[],latestMessageId:null,viewportTop:72,viewportBottom:748,scroller:port===5175?'[data-history-message-window="true"]':'.qwenpaw-bubble-list-scroll',reverseScroll:port===5173,...(scenario==='cold'||scenario==='reload'?{startTime:0}:{})};}
async function setup(port,size,scenario,id,{enabled=true,display='result-collapsed'}={}){
  const context=await browser.newContext({viewport:{width:1440,height:900},deviceScaleFactor:1,colorScheme:'light',locale:'zh-CN'});
  const cfg=config(port,size,scenario,id);
  const init=`Object.entries(${JSON.stringify({...settings,qwenpaw_assistant_message_display_mode:display})}).forEach(([k,v])=>localStorage.setItem(k,v));\n${adapter}\n${enabled?probe:''}\nwindow.perfConfig=${JSON.stringify(cfg)};if(performance.getEntriesByType('navigation')[0]?.type==='reload'){window.perfConfig.scenario='reload';window.perfConfig.sampleId=window.perfConfig.sampleId.replace('-cold-','-reload-');}window.perfConfig.resolveTargets=window.resolvePerfTargets;${enabled?'window.chatPerf.start(window.perfConfig);':''}`;
  await context.addInitScript({content:init});
  const page=await context.newPage();page.setDefaultTimeout(15000);
  const cdp=await context.newCDPSession(page);await cdp.send('Network.enable');await cdp.send('Performance.enable');
  const state={context,page,cdp,config:cfg,network:[],errors:[],port,size};const requests=new Map();
  cdp.on('Network.requestWillBeSent',e=>{const r={id:e.requestId,url:e.request.url,method:e.request.method,start:e.timestamp};requests.set(e.requestId,r);state.network.push(r);});
  cdp.on('Network.responseReceived',e=>{const r=requests.get(e.requestId);if(r)Object.assign(r,{status:e.response.status,mime:e.response.mimeType,fromDiskCache:e.response.fromDiskCache,fromServiceWorker:e.response.fromServiceWorker,response:e.timestamp});});
  cdp.on('Network.requestServedFromCache',e=>{const r=requests.get(e.requestId);if(r)r.servedFromCache=true;});
  cdp.on('Network.loadingFinished',e=>{const r=requests.get(e.requestId);if(r)Object.assign(r,{end:e.timestamp,encodedDataLength:e.encodedDataLength,durationMs:(e.timestamp-r.start)*1000});});
  cdp.on('Network.loadingFailed',e=>{const r=requests.get(e.requestId);if(r)Object.assign(r,{failed:e.errorText,canceled:e.canceled});});
  page.on('pageerror',e=>state.errors.push({type:'pageerror',message:e.message}));
  page.on('console',m=>{if(m.type()==='error')state.errors.push({type:'console',message:m.text()});});
  return state;
}
async function settle(s,timeout=60000){await s.page.waitForFunction(()=>window.chatPerf?.peek()?.firstStableMs!==null&&window.chatPerf?.peek()?.firstStableMs!==undefined,{},{timeout,polling:100});}
async function finish(s,id,status='complete'){
  const data=await s.page.evaluate(status=>{const final=window.chatPerf.checkpoint('end');const result=window.chatPerf.stop(status);const r=window.perfHistory.get(location.pathname.split('/').at(-1));return {result,final,history:r?{ids:r.ids.size,requests:r.requests,lastResponseCount:r.lastResponseCount,latestId:r.latestId,latestOriginalId:r.latestOriginalId}:null,layout:{dpr:devicePixelRatio,visualScale:visualViewport.scale,theme:document.documentElement.getAttribute('data-theme'),settings:Object.fromEntries(Object.keys(localStorage).filter(k=>/display_mode|show_thinking|wide_mode/.test(k)).map(k=>[k,localStorage.getItem(k)]))}};},status);
  data.network=s.network;data.errors=s.errors;data.metrics=await s.cdp.send('Performance.getMetrics');data.metricsBefore=s.metricsBefore;data.browser=browser.version();data.port=s.port;data.size=s.size;
  const expected=verified[s.size].sides[0].originalIds.at(-1);
  data.latestSourceMatches=data.history?.latestOriginalId===expected;
  data.valid=status==='complete'&&data.result.latestVerified&&data.latestSourceMatches&&!Object.values(data.result.truncated).some(Boolean)&&!data.errors.some(e=>e.type==='pageerror');
  const destination=`${mode==='timing'?'raw':'calibration'}/${id}.json`;
  write(fs.existsSync(path.join(out,destination))?`calibration/repeated-${id}-${Date.now()}.json`:destination,data);
  await s.page.screenshot({path:path.join(out,'screenshots',id+'.png'),timeout:15000});
  console.log(JSON.stringify({id,valid:data.valid,first:data.result.firstVisibleMs,latest:data.result.latestVisibleMs,stable:data.result.firstStableMs,dom:data.final.messageElements,messages:data.final.messages.length,errors:data.errors.length}));
  return data;
}
async function measured(s,id,action){s.network=[];s.errors=[];s.metricsBefore=await s.cdp.send('Performance.getMetrics');let status='complete';try{await action();await settle(s);}catch(e){status='timeout';s.errors.push({type:'controller',message:e.message});if(!await s.page.evaluate(()=>Boolean(chatPerf.peek())))await s.page.evaluate(c=>chatPerf.start(c),s.config);}return finish(s,id,status);}
const url=(port,id)=>`http://localhost:${port}/chat/${id}?agent=UP3wQD`;
async function clickSession(s,name){await s.page.getByText(name,{exact:true}).first().click();}
async function startSwitch(s,scenario,id){const cfg=config(s.port,s.size,scenario,id);await s.page.evaluate(c=>{window.chatPerf.stop('setup');c.resolveTargets=window.resolvePerfTargets;const armed=performance.now();document.addEventListener('click',()=>{c.commandDispatchDelayMs=performance.now()-armed;window.chatPerf.start(c);},{capture:true,once:true});},cfg);}
if(mode!=='library')try{
  for(const port of selectedPort?[Number(selectedPort)]:[5173,5175])for(const size of selectedSize?[selectedSize]:mode==='calibration'?['50']:Object.keys(verified)){
    if(!verified[size].equal)continue;
    const repeats=mode==='calibration'?1:3;
    for(let repetition=1;repetition<=repeats;repetition++){
      if(mode==='timing'&&['cold','reload','first-switch','cached-return'].every(scenario=>{const file=path.join(out,`raw/${port}-${size}-${scenario}-${repetition}.json`);return fs.existsSync(file)&&JSON.parse(fs.readFileSync(file)).valid;}))continue;
      let id=`${port}-${size}-cold-${repetition}`;let s=await setup(port,size,'cold',id);
      try{
        await s.cdp.send('Network.clearBrowserCache');
        await measured(s,id,()=>s.page.goto(url(port,verified[size].id),{waitUntil:'domcontentloaded',timeout:60000}));
        if(mode==='calibration')continue;
      }finally{if(mode==='calibration')await s.context.close();}
      id=`${port}-${size}-reload-${repetition}`;
      // Replace the document initializer, retaining the browser resource cache.
      await s.cdp.send('Page.enable');
      // The original init script starts the same navigation config; relabel the result after reload.
      await measured(s,id,()=>s.page.reload({waitUntil:'domcontentloaded',timeout:60000}));
      await s.context.close();
      id=`${port}-${size}-first-switch-${repetition}`;s=await setup(port,size,'first-switch',id);
      try{
        await s.page.goto(url(port,aux),{waitUntil:'domcontentloaded',timeout:60000});
        await s.page.waitForFunction(id=>window.perfHistory.get(id)?.requests>0,aux,{timeout:60000});
        await s.page.waitForTimeout(1000);
        await startSwitch(s,'first-switch',id);
        await measured(s,id,()=>clickSession(s,`Perf Same History - ${size} Turns`));
        await clickSession(s,'CHAT_TEST_OK');
        await s.page.waitForFunction(id=>location.pathname==='/chat/'+id,aux);
        await s.page.waitForTimeout(1200);
        id=`${port}-${size}-cached-return-${repetition}`;
        await startSwitch(s,'cached-return',id);
        await measured(s,id,()=>clickSession(s,`Perf Same History - ${size} Turns`));
      }finally{await s.context.close();}
    }
  }
}finally{await browser.close();}
export {browser,setup,settle,config,measured,finish,out,verified,url,write};
