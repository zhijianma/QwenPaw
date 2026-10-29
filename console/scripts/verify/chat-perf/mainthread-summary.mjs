import fs from 'node:fs';
import path from 'node:path';
const root=path.resolve('console/scripts/verify/artifacts/chat-mainthread-2026-10-29');
const phases=process.argv.slice(2).length?process.argv.slice(2):['baseline','after','production-before','production-after'];
const median=a=>a.sort((a,b)=>a-b)[Math.floor(a.length/2)];
const rows=[];
for(const phase of phases){const dir=path.join(root,phase);if(!fs.existsSync(dir))continue;const all=fs.readdirSync(dir).filter(f=>f.endsWith('.json')).map(f=>JSON.parse(fs.readFileSync(path.join(dir,f)))).filter(x=>x.valid);for(const size of ['50','100','500','1000'])for(const scenario of ['cold','reload']){const a=all.filter(x=>x.size===size&&x.scenario===scenario);if(!a.length)continue;const metric=(x,n)=>x.metrics.metrics.find(m=>m.name===n)?.value;rows.push({phase,size,scenario,n:a.length,latestMs:median(a.map(x=>x.sample.latestVisibleMs)),stableMs:median(a.map(x=>x.sample.firstStableMs)),taskMs:median(a.map(x=>metric(x,'TaskDuration')*1000)),scriptMs:median(a.map(x=>metric(x,'ScriptDuration')*1000)),layoutMs:median(a.map(x=>metric(x,'LayoutDuration')*1000)),styleMs:median(a.map(x=>metric(x,'RecalcStyleDuration')*1000)),heapMiB:median(a.map(x=>metric(x,'JSHeapUsedSize')/1048576))});}}
fs.writeFileSync(path.join(root,'summary.json'),JSON.stringify(rows,null,2));
const fields=Object.keys(rows[0]||{});fs.writeFileSync(path.join(root,'summary.csv'),[fields.join(','),...rows.map(r=>fields.map(f=>r[f]).join(','))].join('\n'));
console.log(JSON.stringify(rows));
