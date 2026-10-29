import fs from 'node:fs';
import path from 'node:path';
import ts from '../../../node_modules/typescript/lib/typescript.js';
import {browser,setup,settle,verified,url} from './run-browser.mjs';
const root=path.resolve('console/scripts/verify/artifacts/chat-mainthread-2026-10-29/diagnostics');
const phase=process.argv[2]||'before';
const sizes=process.argv[3]?[process.argv[3]]:['100','500','1000'];
const instrumented=[];
function instrument(source,file) {
  const ast=ts.createSourceFile(file,source,ts.ScriptTarget.Latest,true,ts.ScriptKind.JS);
  const edits=[];
  const wanted=new Set(['convertMessages','segmentMarkdown','AssistantMarkdownDocument','AssistantMarkdown','measure','anchor','restore','updateRange']);
  const visit=node=>{
    const name=node.name?.getText(ast);
    let body=node.body;
    if(ts.isVariableDeclaration(node)||ts.isPropertyDeclaration(node))body=node.initializer?.body;
    if(name&&wanted.has(name)&&body&&ts.isBlock(body)) {
      const label=file.split('/').at(-1)+':'+name;
      const detail=name==='convertMessages'?'{count:messages.length}':name==='segmentMarkdown'?'{chars:content.length,samePrevious:previous?.source===content,documentScope:/\\[\\^|(?:^|\\n) {0,3}\\[[^\\]\\n]+\\]:|\\$|\\\\\\[|\\\\\\(/.test(content)}':name==='AssistantMarkdown'?'{chars:props.content.length,messageId:props.messageId}':name==='AssistantMarkdownDocument'?'{chars:content.length,messageId}':'{}';
      edits.push({at:body.getStart(ast)+1,text:`const __perfStart=performance.now();try{`},{at:body.end-1,text:`}finally{window.__stages.push({name:${JSON.stringify(label)},start:__perfStart,ms:performance.now()-__perfStart,...${detail}});}`});
      instrumented.push(label);
    }
    ts.forEachChild(node,visit);
  };
  visit(ast);
  for(const e of edits.sort((a,b)=>b.at-a.at))source=source.slice(0,e.at)+e.text+source.slice(e.at);
  if(file.endsWith('/Chat/index.tsx')) {
    const reactPath=source.match(/from ["']([^"']*\/react\.js[^"']*)["']/)?.[1];
    if(!reactPath||!source.includes('export default function ChatPage('))throw new Error('Cannot instrument Chat Profiler');
    source=source.replace('export default function ChatPage(', 'function ChatPage(');
    source+=`\nimport __perfReact from ${JSON.stringify(reactPath)};export default function ProfiledChatPage(){return __perfReact.createElement(__perfReact.Profiler,{id:'ChatPage',onRender:(id,phase,actualDuration,baseDuration,startTime,commitTime)=>window.__commits.push({id,phase,actualDuration,baseDuration,startTime,commitTime})},__perfReact.createElement(ChatPage));}\n`;
    instrumented.push('ChatPage:ReactProfiler');
  }
  if(file.endsWith('/historyMarkdownChunks.ts'))source=source.replace('return { source: content, chunks, stableLength: committed };',"window.__segments.push({chars:content.length,chunks:chunks.map(x=>x.length)});return { source: content, chunks, stableLength: committed };");
  return source;
}
try {
  for(const size of sizes) {
    const s=await setup(5175,size,'cold',`diagnostic-${phase}-${size}`);
    await s.context.addInitScript(()=>{
      window.__stages=[];window.__commits=[];window.__segments=[];
      const fetchOriginal=window.fetch;
      window.fetch=async(...args)=>{
        const response=await fetchOriginal(...args);
        if(/\/api\/chats\/[^/]+(?:\/messages)?(?:\?|$)/.test(response.url)) {
          const json=response.json.bind(response);
          response.json=async()=>{const start=performance.now();try{return await json();}finally{window.__stages.push({name:'response.json (body wait + parse + adapter)',start,ms:performance.now()-start,url:response.url});}};
        }
        return response;
      };
    });
    await s.context.route(/\/src\/pages\/Chat\/(?:sessionApi\/index\.ts|historyMarkdownChunks\.ts|AssistantMarkdown\.tsx|MeasuredMessageWindow\.tsx|index\.tsx)(?:\?|$)/,async route=>{
      const response=await route.fetch();const file=new URL(route.request().url()).pathname;
      await route.fulfill({response,body:instrument(await response.text(),file)});
    });
    const events=[];s.cdp.on('Tracing.dataCollected',e=>events.push(...e.value));
    await s.cdp.send('Network.clearBrowserCache');
    await s.cdp.send('Profiler.enable');await s.cdp.send('Profiler.start');
    await s.cdp.send('Tracing.start',{categories:'devtools.timeline,v8,blink.user_timing,disabled-by-default-devtools.timeline',options:'record-as-much-as-possible'});
    let failure=null;
    try{await s.page.goto(url(5175,verified[size].id),{waitUntil:'domcontentloaded',timeout:90000});await settle(s,90000);}catch(e){failure=e.message;}
    const {profile}=await s.cdp.send('Profiler.stop');
    const ended=new Promise(resolve=>s.cdp.once('Tracing.tracingComplete',resolve));await s.cdp.send('Tracing.end');await ended;
    const data=await s.page.evaluate(()=>({stages:window.__stages,commits:window.__commits,segments:window.__segments,sample:window.chatPerf?.stop()}));
    const metrics=await s.cdp.send('Performance.getMetrics');
    const id=`${phase}-${size}`;
    fs.writeFileSync(path.join(root,id+'.json'),JSON.stringify({failure,...data,metrics,errors:s.errors,network:s.network,instrumented:[...new Set(instrumented)]},null,2));
    fs.writeFileSync(path.join(root,id+'.cpuprofile'),JSON.stringify(profile));
    fs.writeFileSync(path.join(root,id+'-trace.json'),JSON.stringify({traceEvents:events}));
    const totals={};for(const x of data.stages||[]){const a=totals[x.name]??={calls:0,ms:0};a.calls++;a.ms+=x.ms;}
    console.log(JSON.stringify({id,failure,latest:data.sample?.latestVisibleMs,totals,commits:data.commits?.length,errors:s.errors.filter(x=>x.type==='pageerror')}));
    await s.context.close();
    if(failure)throw new Error(failure);
  }
}finally{await browser.close();}
