import {browser,setup,settle,out,url,verified,write} from './run-browser.mjs';
import path from 'node:path';
const ports=process.argv[2]?[Number(process.argv[2])]:[5173,5175];
const sizes=process.argv[3]?[process.argv[3]]:Object.keys(verified);
const displays=process.argv[4]?[process.argv[4]]:['result-collapsed','expanded'];
const rootCDP=await browser.newBrowserCDPSession();
async function point(s,label){return s.page.evaluate(label=>{const p=chatPerf.checkpoint(label);p.visible=p.visibleIds.map(id=>({id,text:document.getElementById(id)?.innerText.slice(0,200)}));p.highlight=[...document.querySelectorAll('[aria-current="true"]')].map(e=>e.innerText);return p;},label);}
try{for(const port of ports)for(const size of sizes)for(const display of displays){
  const id=`navigation-${port}-${size}-${display}${process.env.CHAT_PERF_SUFFIX||''}`;const s=await setup(port,size,'cold',id,{display});const result={id,port,size,display,points:[],operations:[],errors:[]};let lastProgress=Date.now();let killed=false;const save=()=>{lastProgress=Date.now();write(`raw/${id}.json`,{...result,network:s.network,console:s.errors});};
  const watchdog=setInterval(async()=>{if(!killed&&Date.now()-lastProgress>60000){killed=true;result.errors.push({error:'Renderer unresponsive: no checkpoint for 60 seconds'});const info=await rootCDP.send('SystemInfo.getProcessInfo');result.processAtTimeout=info;for(const p of info.processInfo.filter(p=>p.type==='renderer')){try{process.kill(p.id,'SIGTERM')}catch{}}}},5000);
  try{
    await s.page.goto(url(port,verified[size].id),{waitUntil:'domcontentloaded',timeout:60000});await settle(s);await s.page.waitForTimeout(2500);
    await s.page.evaluate(c=>{chatPerf.stop();delete c.startTime;c.scenario='navigation';c.resolveTargets=resolvePerfTargets;chatPerf.start(c)},s.config);
    if(size==='1000'&&display==='expanded'){
      const previous=s.page.getByRole('button',{name:/^(Scroll to )?previous user message$/i});
      for(let index=1;index<=2;index++){if(await previous.isEnabled()){await previous.click();await s.page.waitForTimeout(900);result.points.push(await point(s,`extreme-turn-previous-${index}`));}}
      await s.page.mouse.move(1000,400);
      for(let step=1;step<=20;step++){await s.page.mouse.wheel(0,1200);await s.page.waitForTimeout(300);if([1,5,10,20].includes(step)){result.points.push(await point(s,`extreme-turn-step-${step}`));await s.page.screenshot({path:path.join(out,'screenshots',id+`-extreme-${step}.png`)});save();}}
      for(let step=1;step<=20;step++){await s.page.mouse.wheel(0,-1200);await s.page.waitForTimeout(300);if([1,5,10,20].includes(step)){result.points.push(await point(s,`extreme-turn-reverse-${step}`));await s.page.screenshot({path:path.join(out,'screenshots',id+`-extreme-reverse-${step}.png`)});save();}}
    }
    for(let repeat=1;repeat<=2;repeat++){
      await s.page.getByRole('button',{name:/^(Open )?user message directory$/i}).click();
      const list=s.page.locator('[class*="navigationDirectoryList"], [class$="-anchor-directory-list"]').filter({visible:true}).first();
      await list.waitFor({state:'visible'});await list.hover();await s.page.mouse.wheel(0,-5000);await s.page.waitForTimeout(800);
      const target=list.locator('button').first();const selected=await target.evaluate(e=>{const f=e[Object.keys(e).find(k=>k.startsWith('__reactFiber'))];return {text:e.innerText,id:f?.key};});
      const t=Date.now();await target.click();
      if(selected.id){try{await s.page.waitForFunction(id=>{const e=document.getElementById(id);if(!e)return false;const r=e.getBoundingClientRect();return r.height>0&&r.bottom>72&&r.top<748;},selected.id,{timeout:10000,polling:100});}catch(e){result.errors.push({repeat,type:'navigation-target-not-visible',selected,error:e.message});}}
      await s.page.waitForTimeout(600);
      const p=await point(s,`repeat-${repeat}-far-navigation`);result.points.push(p);result.operations.push({type:'far-navigation',repeat,selected,elapsedMs:Date.now()-t,visible:p.visibleIds.includes(selected.id)});save();
      await s.page.screenshot({path:path.join(out,'screenshots',id+`-r${repeat}-far.png`)});
      if(selected.id&&!p.visibleIds.includes(selected.id)){
        result.errors.push({repeat,type:'navigation-anchor-lost-after-visible',selected});
        await s.page.waitForTimeout(3000);result.points.push(await point(s,`repeat-${repeat}-anchor-after-3s`));await s.page.screenshot({path:path.join(out,'screenshots',id+`-r${repeat}-anchor-after-3s.png`)});save();
      }
      // The SDK directory stays open after selection and intercepts wheel input.
      if(await list.isVisible())await s.page.getByRole('button',{name:/^(Open )?user message directory$/i}).click();
      await s.page.waitForTimeout(200);
      // Real wheel input between directory navigation and adjacent-message checks.
      await s.page.mouse.move(800,400);
      const initialRound=Number(selected.text.match(/History Performance (\d+)\//)?.[1]);
      const milestones=new Set();
      let consecutiveBoundaries=0;
      for(let wheel=1;wheel<=200;wheel++){
        const start=Date.now();const delta=display==='expanded'?5000:650;await s.page.mouse.wheel(0,delta);await s.page.waitForTimeout(250);
        const visibleRounds=await s.page.evaluate(selector=>[...document.querySelector(selector).querySelectorAll('[data-role="user"][id]')].filter(e=>{const r=e.getBoundingClientRect();return r.bottom>72&&r.top<748;}).map(e=>Number(e.textContent.match(/History Performance (\d+)\//)?.[1])).filter(Number.isFinite),s.config.scroller);
        const advanced=Math.max(0,...visibleRounds)-initialRound;
        for(const milestone of [5,10,20])if(advanced>=milestone&&!milestones.has(milestone)){milestones.add(milestone);await s.page.waitForTimeout(500);result.points.push(await point(s,`repeat-${repeat}-round-${milestone}`));await s.page.screenshot({path:path.join(out,'screenshots',id+`-r${repeat}-round${milestone}.png`)});}
        result.operations.push({type:'wheel',repeat,wheel,deltaY:delta,elapsedMs:Date.now()-start,initialRound,visibleRounds,advanced});save();
        if(advanced>=20)break;
        const bottomReached=await s.page.evaluate(selector=>{const e=document.querySelector(selector);return getComputedStyle(e).flexDirection==='column-reverse'?Math.abs(e.scrollTop)<24:e.scrollHeight-e.clientHeight-e.scrollTop<24;},s.config.scroller);
        if(bottomReached){
          await s.page.waitForTimeout(1500);
          const boundary=await point(s,`repeat-${repeat}-boundary-wait-${wheel}`);result.points.push(boundary);
          const stillBottom=boundary.scroll&&(port===5173?Math.abs(boundary.scroll.top)<24:boundary.scroll.height-boundary.scroll.clientHeight-boundary.scroll.top<24);
          consecutiveBoundaries=stillBottom?consecutiveBoundaries+1:0;
          if(consecutiveBoundaries>=3){result.errors.push({repeat,type:'cannot-continue-after-boundary',initialRound,advanced});break;}
        }else consecutiveBoundaries=0;
        if(!initialRound&&wheel>=20)break;
      }
      for(let reverse=1;reverse<=5;reverse++){await s.page.mouse.wheel(0,display==='expanded'?-2400:-650);await s.page.waitForTimeout(400);result.points.push(await point(s,`repeat-${repeat}-reverse-${reverse}`));}
      const bottom=s.page.getByRole('button',{name:'Scroll to bottom',exact:true});
      const fallback=s.page.locator('.qwenpaw-bubble-list-scroll-to-bottom-show');
      const start=Date.now();if(await bottom.isVisible())await bottom.click();else if(await fallback.isVisible())await fallback.click();else {const alreadyBottom=await s.page.evaluate(selector=>{const e=document.querySelector(selector);return getComputedStyle(e).flexDirection==='column-reverse'?Math.abs(e.scrollTop)<24:e.scrollHeight-e.clientHeight-e.scrollTop<24;},s.config.scroller);if(!alreadyBottom)result.errors.push({repeat,error:'Bottom button absent while away from bottom'});}
      await s.page.waitForTimeout(300);
      let latestReady=true;
      try{await s.page.waitForFunction(selector=>{const list=document.querySelector(selector);const elements=[...list.querySelectorAll('[data-role][id]')];const c={path:location.pathname,expectedMessageIds:[]};resolvePerfTargets(elements,c);const target=elements.find(e=>e.id===c.latestMessageId);if(!target)return false;const r=target.getBoundingClientRect();const bottom=getComputedStyle(list).flexDirection==='column-reverse'?Math.abs(list.scrollTop)<24:list.scrollHeight-list.clientHeight-list.scrollTop<24;return r.height>0&&r.bottom>72&&r.top<748&&bottom&&list.dataset.historyLoading!=='true';},s.config.scroller,{timeout:15000,polling:100});await s.page.waitForTimeout(500);}catch(e){latestReady=false;result.errors.push({repeat,type:'return-latest-timeout',error:e.message});}
      const returned=await point(s,`repeat-${repeat}-bottom`);result.points.push(returned);result.operations.push({type:'bottom',repeat,elapsedMs:Date.now()-start,latestReady,scroll:returned.scroll});
      await s.page.screenshot({path:path.join(out,'screenshots',id+`-r${repeat}-bottom.png`)});save();
    }
    result.sample=await s.page.evaluate(()=>chatPerf.stop());result.status='complete';
  }catch(e){result.status='incomplete';result.errors.push({error:e.message});try{await s.page.screenshot({path:path.join(out,'screenshots',id+'-failure.png')});}catch{}}
  finally{clearInterval(watchdog);save();await s.context.close();}
  console.log(JSON.stringify({id,status:result.status,errors:result.errors.length,points:result.points.length}));
}}finally{await browser.close();}
