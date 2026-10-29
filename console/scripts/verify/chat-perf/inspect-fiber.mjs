import { chromium } from '/Users/mazhijian/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs';
import fs from 'node:fs';
const browser=await chromium.launch({channel:'chrome',headless:true});
const page=await browser.newPage({viewport:{width:1440,height:900}});
await page.goto('http://localhost:5173/chat/fd6b78ba-474e-422f-a093-8a3bdfc5cd6b?agent=UP3wQD');
await page.waitForSelector('[data-role="assistant"][id]');
await page.waitForTimeout(2000);
const state=await page.evaluate(()=>{const el=document.querySelector('[data-role="assistant"][id]');let f=el[Object.keys(el).find(k=>k.startsWith('__reactFiber'))];const a=[];for(let i=0;f&&i<12;i++,f=f.return){const p=f.memoizedProps; a.push({type:typeof f.type==='string'?f.type:f.type?.name,keys:Object.keys(p||{}),id:p?.id, data:JSON.stringify(p?.data||p?.message||p?.item||null)?.slice(0,1200)});}return {fiber:a,buttons:[...document.querySelectorAll('button')].map(e=>({text:e.textContent,aria:e.getAttribute('aria-label'),title:e.title})),targets:[...document.querySelectorAll('[id^="session-target:"]')].map(e=>({id:e.id,text:e.textContent}))};});
fs.writeFileSync('console/scripts/verify/artifacts/chat-perf-2026-10-28/calibration/fiber.json',JSON.stringify(state,null,2));console.log(JSON.stringify(state));await browser.close();
