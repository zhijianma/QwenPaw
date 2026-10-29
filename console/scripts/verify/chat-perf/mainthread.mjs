import fs from 'node:fs';
import path from 'node:path';
import {browser,setup,settle,verified,url} from './run-browser.mjs';
const root=path.resolve('console/scripts/verify/artifacts/chat-mainthread-2026-10-29');
const phase=process.argv[2]||'baseline';
const port=Number(process.argv[3]||5175);
const sizes=process.argv[4]?[process.argv[4]]:['100','500','1000','50'];
const directory=path.join(root,phase);fs.mkdirSync(directory,{recursive:true});
try {
  for(const size of sizes)for(let repeat=1;repeat<=3;repeat++) {
    const s=await setup(5175,size,'cold',`${phase}-${size}-cold-${repeat}`);
    try {
      await s.cdp.send('Network.clearBrowserCache');
      for(const scenario of ['cold','reload']) {
        const id=`${phase}-${size}-${scenario}-${repeat}`;
        s.network=[];s.errors=[];
        let failure=null;
        try {
          if(scenario==='cold')await s.page.goto(url(port,verified[size].id),{waitUntil:'domcontentloaded',timeout:90000});
          else await s.page.reload({waitUntil:'domcontentloaded',timeout:90000});
          await settle(s,90000);
        }catch(error){failure=error.message;}
        const data=await s.page.evaluate(()=>({sample:window.chatPerf?.stop(),history:window.perfHistory?.get(location.pathname.split('/').at(-1))?{latestOriginalId:window.perfHistory.get(location.pathname.split('/').at(-1)).latestOriginalId}:null}));
        const metrics=await s.cdp.send('Performance.getMetrics');
        const result={id,phase,port,size,repeat,scenario,failure,...data,metrics,network:s.network,errors:s.errors,browser:browser.version()};
        result.valid=!failure&&data.sample?.latestVerified&&data.history?.latestOriginalId===verified[size].sides[0].originalIds.at(-1)&&!s.errors.some(x=>x.type==='pageerror')&&!Object.values(data.sample?.truncated||{}).some(Boolean);
        fs.writeFileSync(path.join(directory,id+'.json'),JSON.stringify(result,null,2));
        if(repeat===1)await s.page.screenshot({path:path.join(root,'screenshots',id+'.png'),timeout:15000});
        console.log(JSON.stringify({id,valid:result.valid,latest:data.sample?.latestVisibleMs,stable:data.sample?.firstStableMs,task:metrics.metrics.find(x=>x.name==='TaskDuration')?.value,failure}));
        if(!result.valid)throw new Error('Invalid sample: '+id);
      }
    } finally {await s.context.close();}
  }
} finally {await browser.close();}
