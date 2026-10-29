import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { execFileSync } from 'node:child_process';
const baseOut = path.resolve('console/scripts/verify/artifacts/chat-perf-2026-10-28');
const after = process.argv.includes('--after');
const out = after ? path.join(baseOut,'calibration','after') : baseOut;
fs.mkdirSync(path.join(out,'calibration'),{recursive:true});
const groups = {50:'fd6b78ba-474e-422f-a093-8a3bdfc5cd6b',100:'6f0cb439-2b27-467d-84b1-119a74b05641',500:'1d80e02d-7c20-43fa-a89a-28544f8a4ae6',1000:'fe092757-705b-4b7d-a16d-cb1c9e488314'};
const write = (name,data) => fs.writeFileSync(path.join(out,name),JSON.stringify(data,null,2));
const canonical = x => Array.isArray(x) ? x.map(canonical) : x && typeof x === 'object' ? Object.fromEntries(Object.keys(x).sort().map(k=>[k,canonical(x[k])])) : x;
const hash = x => crypto.createHash('sha256').update(JSON.stringify(canonical(x))).digest('hex');
const run = (cmd,args) => execFileSync(cmd,args,{encoding:'utf8'}).trim();
const env = {date:new Date().toISOString(),os:run('sw_vers',[]),cpu:run('sysctl',['-n','machdep.cpu.brand_string']),memory:run('sysctl',['-n','hw.memsize']),viewport:{width:1440,height:900},projects:{}};
for (const dir of ['../QwenPaw','.']) env.projects[dir]={commit:run('git',['-C',dir,'rev-parse','HEAD']),status:run('git',['-C',dir,'status','--short']),diffHash:hash(run('git',['-C',dir,'diff','--binary']))};
if(!fs.existsSync(path.join(out,'environment.json')))write('environment.json',env);
async function get(port,id,suffix='') { const r=await fetch(`http://localhost:${port}/api/chats/${id}${suffix}`,{headers:{'X-Agent-Id':'UP3wQD'}}); if(!r.ok)throw Error(`${r.status} ${suffix}`); return r.json(); }
const summary={};
for(const [size,id] of Object.entries(groups)){
  const sides=[];
  for(const port of [8001,8003]){
    const cached=path.join(out,`raw/data-${port}-${size}.json`);
    let page=!after&&fs.existsSync(cached)?{messages:JSON.parse(fs.readFileSync(cached))}:await get(port,id); let messages=page.messages; let history=page.history; let requests=!after&&fs.existsSync(cached)?null:1;
    while(history?.has_more){page=await get(port,id,'/messages?'+new URLSearchParams({before:history.next_before,limit:'100',max_bytes:'4194304'}));messages=page.messages.concat(messages);history=page;requests++;if(requests>10000)throw Error('Pagination exceeded bound');}
    if(!after)write(`raw/data-${port}-${size}.json`,messages);
    const merged=[];
    for(const m of messages){const f=m.metadata?.qwenpaw_history_fragment;if(!f){merged.push(m);continue;}const prev=merged.at(-1);if(f.content_start===0){if(m.content.length!==f.content_end)throw Error('Invalid first fragment');merged.push(structuredClone(m));}else{if(prev?.id!==m.id||prev.content.length!==f.content_start)throw Error('Missing or overlapping fragment');prev.content.push(...m.content);}}
    for(const m of merged){const f=m.metadata?.qwenpaw_history_fragment;if(f&&m.content.length!==f.content_total)throw Error('Incomplete message');}
    const wireCount=messages.length;messages=merged;
    const normalized=messages.map(m=>({id:m.metadata?.original_id||m.id,type:m.type,role:m.role,content:m.content,status:m.status}));
    const counts={};for(const m of messages) counts[m.role+':'+m.type]=(counts[m.role+':'+m.type]||0)+1;
    sides.push({port,count:messages.length,wireCount,counts,requests,hash:hash(normalized),ids:messages.map(m=>m.id),originalIds:normalized.map(m=>m.id),messageHashes:normalized.map(hash),latestMessageId:messages.at(-1)?.id});
  }
  const [a,b]=sides;const mismatches=[];for(let i=0;i<Math.max(a.count,b.count);i++)if(a.messageHashes[i]!==b.messageHashes[i]){if(mismatches.length<10)mismatches.push({index:i,old:a.originalIds[i],current:b.originalIds[i]});}
  summary[size]={id,equal:a.hash===b.hash,sides,mismatches};write('calibration/data-consistency.json',summary);
  console.log(JSON.stringify({size,equal:a.hash===b.hash,counts:sides.map(x=>x.count),requests:sides.map(x=>x.requests),mismatches}));
}
