import React, { useState, useRef } from 'react';
import { createRoot } from 'react-dom/client';
import XMarkdown from '@ant-design/x-markdown';
import { RenderableCodeBlock } from '../../src/components/RenderableCodeBlock/RenderableCodeBlock';
import { WindowedMarkdownTable, WindowedMarkdownTableBody } from '../../src/pages/Chat/WindowedMarkdownTable';
const source = '/* Request validation: preserve multiline syntax.\n' + Array.from({length:3000}, (_,i) => ` * Request ${i}: validate workspace and message identity before dispatch.`).join('\n') + '\n*/\nexport const complete = true;';
const table = '| Module | Status | Details |\n| --- | --- | --- |\n' + Array.from({length:2000},(_,i)=>`| module-${i} | verified | Validate cursor and preserve scroll anchor for request ${i}. |`).join('\n');
function App() {
 const [kind,setKind]=useState('code'); const [bounded,setBounded]=useState(true); const ref=useRef<HTMLDivElement>(null);
 return <><h2>Chat Markdown verification: 3003 code lines / 2000 table rows</h2><button onClick={()=>setKind('code')}>Code</button><button onClick={()=>setKind('table')}>Table</button><button onClick={()=>setBounded(v=>!v)}>{bounded?'Bounded':'Baseline'}</button><button onClick={()=>{if(ref.current) ref.current.scrollTop=0}}>Top</button><button onClick={()=>{if(ref.current) ref.current.scrollTop=ref.current.scrollHeight/2}}>Middle</button><button onClick={()=>{if(ref.current) ref.current.scrollTop=ref.current.scrollHeight}}>Bottom</button><div ref={ref} data-history-message-window="true" style={{height:'70vh',overflow:'auto',border:'1px solid #ddd',marginTop:16}}>{kind==='code'?<RenderableCodeBlock block lang="javascript" virtualize={bounded}>{source}</RenderableCodeBlock>:<XMarkdown content={table} components={bounded?{table:WindowedMarkdownTable,tbody:WindowedMarkdownTableBody}:undefined}/>}</div></>;
}
createRoot(document.getElementById('root')!).render(<App/>);
