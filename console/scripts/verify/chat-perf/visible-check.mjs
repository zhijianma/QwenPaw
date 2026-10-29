import {browser,setup,settle,out,url,verified,write} from './run-browser.mjs';
import path from 'node:path';
const records=[];
const rootCDP=await browser.newBrowserCDPSession();
try{for(const port of [5173,5175])for(const size of Object.keys(verified)){
  const id=`visible-chrome-${port}-${size}`;const s=await setup(port,size,'cold',id);const record={port,size,headful:process.env.CHAT_PERF_HEADFUL==='1',points:[]};
  const watchdog=setTimeout(async()=>{record.watchdog=true;const info=await rootCDP.send('SystemInfo.getProcessInfo');for(const p of info.processInfo.filter(p=>p.type==='renderer')){try{process.kill(p.id,'SIGTERM')}catch{}}},90000);
  try{
    await s.page.goto(url(port,verified[size].id),{waitUntil:'domcontentloaded',timeout:60000});await s.page.bringToFront();await settle(s);
    record.load=await s.page.evaluate(()=>chatPerf.peek());record.points.push(await s.page.evaluate(()=>chatPerf.checkpoint('visible-browser-loaded')));
    await s.page.screenshot({path:path.join(out,'screenshots',id+'-loaded.png')});
    await s.page.mouse.move(1000,400);for(let n=0;n<5;n++){await s.page.mouse.wheel(0,-650);await s.page.waitForTimeout(600);}
    record.points.push(await s.page.evaluate(()=>chatPerf.checkpoint('visible-browser-scrolled-up')));
    await s.page.screenshot({path:path.join(out,'screenshots',id+'-scrolled.png')});
    const directory=s.page.getByRole('button',{name:/^(Open )?user message directory$/i});
    await directory.click();await s.page.waitForTimeout(300);
    record.directoryHighlight=await s.page.evaluate(()=>[...document.querySelectorAll('[aria-current="true"]')].map(e=>({text:e.innerText,rect:e.getBoundingClientRect().toJSON()})));
    await s.page.screenshot({path:path.join(out,'screenshots',id+'-highlight.png')});await directory.click();
    const button=s.page.getByRole('button',{name:'Scroll to bottom',exact:true});if(await button.count())await button.click();else await s.page.locator('.qwenpaw-bubble-list-scroll-to-bottom-show').click();
    await s.page.waitForTimeout(1000);record.points.push(await s.page.evaluate(()=>chatPerf.checkpoint('visible-browser-returned-bottom')));record.status='complete';
    await s.page.screenshot({path:path.join(out,'screenshots',id+'-bottom.png')});
  }catch(e){record.status='incomplete';record.error=e.message;}
  clearTimeout(watchdog);record.network=s.network;record.errors=s.errors;records.push(record);write('raw/visible-browser-checks.json',records);console.log(JSON.stringify({port,size,status:record.status}));await s.context.close();
}}finally{await browser.close();}
