import fs from 'node:fs';
import path from 'node:path';
const out=path.resolve('console/scripts/verify/artifacts/chat-perf-2026-10-28');
const read=name=>JSON.parse(fs.readFileSync(path.join(out,name)));
const median=a=>{a=[...a].sort((a,b)=>a-b);return a.length?a[Math.floor(a.length/2)]:null;};
const stat=a=>({median:median(a),min:a.length?Math.min(...a):null,max:a.length?Math.max(...a):null});
const rows=[];
for(const file of fs.readdirSync(path.join(out,'raw')).filter(f=>/^517[35]-\d+-(cold|reload|first-switch|cached-return)-\d+\.json$/.test(f))){
  const x=read('raw/'+file);const c=x.result.config;const metric=(a,n)=>a?.metrics.find(m=>m.name===n)?.value??0;const duration=n=>1000*(metric(x.metrics,n)-(['cold','reload'].includes(c.scenario)?0:metric(x.metricsBefore,n)));
  const history=x.network.filter(r=>r.method==='GET'&&new RegExp('/api/chats/'+x.result.config.path.split('/').at(-1)+'(?:/messages)?(?:\\?|$)').test(r.url));
  rows.push({sampleId:c.sampleId,port:x.port,size:x.size,scenario:c.scenario,valid:x.valid,firstVisibleMs:x.result.firstVisibleMs,latestVisibleMs:x.result.latestVisibleMs,firstStableMs:x.result.firstStableMs,documentElements:x.final.documentElements,messageElements:x.final.messageElements,mounted:x.final.messages.length,heapMiB:x.final.jsHeapBytes/1048576,taskMs:duration('TaskDuration'),scriptMs:duration('ScriptDuration'),layoutMs:duration('LayoutDuration'),styleMs:duration('RecalcStyleDuration'),historyRequests:history.length,historyNetworkSumMs:history.reduce((sum,r)=>sum+(r.durationMs||0),0),historyEncodedBytes:history.reduce((sum,r)=>sum+(r.encodedDataLength||0),0),longTasks:x.result.longTasks.length,longTaskMs:x.result.longTasks.reduce((sum,r)=>sum+r.duration,0),probeCostMs:x.final.probeCostMs,injectionLateHeuristic:x.result.injectionLate});
}
rows.sort((a,b)=>a.port-b.port||Number(a.size)-Number(b.size)||a.scenario.localeCompare(b.scenario)||a.sampleId.localeCompare(b.sampleId));
const aggregates=[];
for(const port of [5173,5175])for(const size of ['50','100','500','1000'])for(const scenario of ['cold','reload','first-switch','cached-return']){const a=rows.filter(x=>x.port===port&&x.size===size&&x.scenario===scenario&&x.valid);const metrics={};for(const k of ['firstVisibleMs','latestVisibleMs','firstStableMs','taskMs','scriptMs','layoutMs','styleMs','heapMiB','historyRequests','historyNetworkSumMs','historyEncodedBytes'])metrics[k]=stat(a.map(x=>x[k]));aggregates.push({port,size,scenario,n:a.length,...metrics});}
const csv=(filename,data)=>{if(!data.length)return;const keys=Object.keys(data[0]);const cell=x=>JSON.stringify(x??'');fs.writeFileSync(path.join(out,filename),[keys.join(','),...data.map(r=>keys.map(k=>cell(r[k])).join(','))].join('\n'));};
csv('raw/timing.csv',rows);fs.writeFileSync(path.join(out,'summary.json'),JSON.stringify({rows,aggregates},null,2));
const points=[];for(const file of fs.readdirSync(path.join(out,'raw')).filter(f=>/^scroll-517[35]-\d+\.json$/.test(f))){const x=read('raw/'+file);for(let i=0;i<x.points.length;i++){const p=x.points[i];points.push({port:x.port,size:x.size,index:i,label:p.label,timeMs:p.timeMs,documentElements:p.documentElements,messageElements:p.messageElements,users:p.messages.filter(m=>m.role==='user').length,assistants:p.messages.filter(m=>m.role==='assistant').length,mounted:p.messages.length,internalRows:p.windows.filter(w=>w.nested).reduce((n,w)=>n+w.rowIds.length,0),outerRows:p.windows.filter(w=>!w.nested).reduce((n,w)=>n+w.rowIds.length,0),heapMiB:p.jsHeapBytes/1048576,requestedPages:p.historyRequests,loadedIds:p.loadedIds,probeCostMs:p.probeCostMs,settleMs:p.settleMs??null,stable:p.stable??null});}}
csv('raw/scroll.csv',points);console.log(JSON.stringify({timing:rows.length,valid:rows.filter(x=>x.valid).length,scrollPoints:points.length}));
