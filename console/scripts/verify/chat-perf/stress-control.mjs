import {browser,setup,out,url,verified,write} from './run-browser.mjs';
import path from 'node:path';
const rootCDP=await browser.newBrowserCDPSession();
const rows=[];
try{for(const size of ['500','1000'])for(const enabled of [false,true]){
  const id=`stress-control-5173-${size}-${enabled?'on':'off'}`;const s=await setup(5173,size,'cold',id,{enabled});const row={id,size,enabled};
  let timer;const deadline=new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('60 second control deadline exceeded')),60000);});
  try{await Promise.race([(async()=>{
    await s.page.goto(url(5173,verified[size].id),{waitUntil:'domcontentloaded',timeout:60000});await s.page.waitForTimeout(20000);
    row.state=await s.page.evaluate(()=>({elements:document.querySelectorAll('*').length,messages:document.querySelectorAll('[data-role][id]').length,heap:performance.memory.usedJSHeapSize,probe:window.chatPerf?.peek(),now:performance.now()}));
    row.metrics=await s.cdp.send('Performance.getMetrics');await s.page.screenshot({path:path.join(out,'screenshots',id+'.png'),timeout:5000});row.status='responsive';
  })(),deadline]);}catch(e){row.status='unresponsive';row.error=e.message;row.processes=await rootCDP.send('SystemInfo.getProcessInfo');for(const p of row.processes.processInfo.filter(p=>p.type==='renderer')){try{process.kill(p.id,'SIGTERM')}catch{}}}
  finally{clearTimeout(timer);row.network=s.network;row.console=s.errors;rows.push(row);write('calibration/stress-controls.json',rows);await s.context.close();}
  console.log(JSON.stringify({id,status:row.status,messages:row.state?.messages}));
}}finally{await browser.close();}
