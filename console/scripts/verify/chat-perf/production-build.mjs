import fs from 'node:fs';
import path from 'node:path';
import { build } from '../../../node_modules/vite/dist/node/index.js';
const root=path.resolve('console/scripts/verify/artifacts/chat-mainthread-2026-10-29');
const phase=process.argv[2]||'before';
const originals=new Map(['src/components/MermaidCodeBlock/MermaidCodeBlock.tsx','src/components/RenderableCodeBlock/RenderableCodeBlock.tsx','src/components/Chat/ToolCards/shared/DefaultBlock.tsx'].map(f=>[path.resolve('console',f),path.join(root,'baseline/source/console',f+'.txt')]));
await build({root:path.resolve('console'),mode:'production',build:{outDir:path.join(root,'production',phase+'-dist')},plugins:phase==='before'?[{name:'verify-baseline-sources',enforce:'pre',load(id){const original=originals.get(id);if(original)return fs.readFileSync(original,'utf8');}}]:[]});
