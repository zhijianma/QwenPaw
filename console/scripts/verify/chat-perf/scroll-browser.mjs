import {browser,setup,settle,config,out,url,verified,write} from './run-browser.mjs';
import {execFileSync} from 'node:child_process';
import path from 'node:path';
const browserCDP=await browser.newBrowserCDPSession();
const portFilter=process.argv[2];const sizeFilter=process.argv[3];
async function processStats(){const {processInfo}=await browserCDP.send('SystemInfo.getProcessInfo');return processInfo.map(p=>{let rssKiB=null;try{rssKiB=Number(execFileSync('ps',['-p',String(p.id),'-o','rss='],{encoding:'utf8'}).trim())}catch{}return {...p,rssKiB};});}
async function waitGeometry(s){await s.page.evaluate(()=>{window.scrollStable=null;});await s.page.waitForFunction(selector=>{const e=document.querySelector(selector);if(!e)return false;const value=[e.scrollTop,e.scrollHeight,e.clientHeight,e.dataset.historyLoading,[...e.querySelectorAll('[data-role][id]')].map(x=>x.id).join(',')].join('|');const prev=window.scrollStable;if(!prev||prev.value!==value||e.dataset.historyLoading==='true'){window.scrollStable={value,since:performance.now()};return false;}return performance.now()-prev.since>=500;},s.config.scroller,{timeout:12000,polling:100});}
async function snapshot(s,label){const point=await s.page.evaluate(label=>{const point=chatPerf.checkpoint(label);const record=perfHistory.get(location.pathname.split('/').at(-1));point.loadedIds=record?.ids.size;point.historyRequests=record?.requests;point.visibleText=point.visibleIds.map(id=>({id,text:document.getElementById(id)?.innerText.slice(0,180)}));return point},label);point.historyNetworkRequests=s.network.filter(r=>r.method==='GET'&&/\/api\/chats\/[^/]+(?:\/messages)?(?:\?|$)/.test(r.url)).length;point.cdp=await s.cdp.send('Performance.getMetrics');return point;}
async function atBottom(s){return s.page.evaluate(selector=>{const e=document.querySelector(selector);return e&&(getComputedStyle(e).flexDirection==='column-reverse'?Math.abs(e.scrollTop)<24:e.scrollHeight-e.clientHeight-e.scrollTop<24);},s.config.scroller);}
try{for(const port of portFilter?[Number(portFilter)]:[5173,5175])for(const size of sizeFilter?[sizeFilter]:Object.keys(verified)){
  const id=`scroll-${port}-${size}`;const s=await setup(port,size,'cold',id);const result={id,port,size,points:[],errors:[],processBefore:await processStats()};
  let lastProgress=Date.now();let killed=false;
  const save=()=>{lastProgress=Date.now();write(`raw/${id}.json`,{...result,network:s.network,console:s.errors});};
  const watchdog=setInterval(async()=>{if(!killed&&Date.now()-lastProgress>60000){killed=true;result.errors.push({error:'Renderer unresponsive: no checkpoint for 60 seconds'});result.processAtTimeout=await processStats();for(const p of result.processAtTimeout.filter(p=>p.type==='renderer')){try{process.kill(p.id,'SIGTERM')}catch{}}}},5000);
  try{
    await s.page.goto(url(port,verified[size].id),{waitUntil:'domcontentloaded',timeout:60000});await settle(s);
    result.firstStable=await snapshot(s,'first-stable');
    // Observe delayed mounting without additional scrolling.
    for(const seconds of [2,5,10]){await s.page.waitForTimeout(seconds*1000);result.points.push(await snapshot(s,`idle-plus-${seconds}s`));save();}
    await s.page.evaluate(c=>{chatPerf.stop('setup');delete c.startTime;c.scenario='scroll';c.resolveTargets=resolvePerfTargets;chatPerf.start(c);},s.config);
    result.points.push(await snapshot(s,'initial'));await s.page.mouse.move(1000,400);
    for(let cycle=1;cycle<=2;cycle++){
      for(const direction of [-1,1]){
        const startPoint=await snapshot(s,`cycle-${cycle}-direction-${direction}-start`);
        const visited=new Set();
        const limit=200;
        for(let page=1;page<=limit;page++){
          const delta=await s.page.evaluate(selector=>{const list=document.querySelector(selector);const heights=[...list.querySelectorAll('[data-role="assistant"][id]')].map(e=>e.getBoundingClientRect()).filter(r=>r.bottom>72&&r.top<748).map(r=>r.height);return Math.max(...heights,0)>5000?15000:650;},s.config.scroller);
          const start=Date.now();await s.page.mouse.wheel(0,direction*delta);
          let stable=true;try{await waitGeometry(s);}catch(e){stable=false;result.errors.push({cycle,direction,page,error:e.message});}
          const point=await snapshot(s,`cycle-${cycle}-${direction===-1?'up':'down'}-${page}`);point.settleMs=Date.now()-start;point.stable=stable;point.wheelDelta=direction*delta;result.points.push(point);save();
          for(const visible of point.visibleText)visited.add(visible.id);
          if([1,5,10,20].includes(page))await s.page.screenshot({path:path.join(out,'screenshots',`${id}-c${cycle}-${direction}-${page}.png`)});
          if(direction===1&&await atBottom(s))break;
          if(direction===-1&&point.visibleText.some(x=>x.text?.includes('[History Performance 0001/')))break;
          if(direction===-1&&page>=20&&(port===5175?point.historyNetworkRequests-startPoint.historyNetworkRequests>=20:visited.size>=200))break;
        }
        result.points.push(await snapshot(s,`cycle-${cycle}-${direction===-1?'top':'bottom'}`));result.processAtBoundary=await processStats();save();
      }
    }
    result.sample=await s.page.evaluate(()=>chatPerf.stop());result.processAfter=await processStats();result.status='complete';
  }catch(e){result.status='incomplete';result.errors.push({error:e.message});try{await s.page.screenshot({path:path.join(out,'screenshots',id+'-failure.png')});}catch{}}
  finally{clearInterval(watchdog);save();await s.context.close();}
  console.log(JSON.stringify({id,status:result.status,points:result.points.length,errors:result.errors.length,mountedRange:[Math.min(...result.points.map(p=>p.messages.length)),Math.max(...result.points.map(p=>p.messages.length))]}));
}}finally{await browser.close();}
