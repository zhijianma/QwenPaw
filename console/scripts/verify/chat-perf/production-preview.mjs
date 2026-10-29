import path from 'node:path';
import { preview } from '../../../node_modules/vite/dist/node/index.js';
const directory=path.resolve('console/scripts/verify/artifacts/chat-mainthread-2026-10-29/production',process.argv[2]||'before-dist');
const server=await preview({root:path.resolve('console'),build:{outDir:directory},preview:{host:'127.0.0.1',port:Number(process.argv[3]||5185),strictPort:true,proxy:{'/api':{target:'http://127.0.0.1:8003',changeOrigin:false}}}});
server.printUrls();
