import fs from 'node:fs';
import path from 'node:path';
import {chromium} from '/Users/mazhijian/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs';
const root=path.resolve('console/scripts/verify/artifacts/chat-mainthread-2026-10-29');
const browser=await chromium.launch({channel:'chrome',headless:false});
const page=await browser.newPage({viewport:{width:1440,height:900}});
const errors=[];page.on('pageerror',e=>errors.push(e.message));
const html=`<!doctype html><html><body><div id="root"></div><script type="module">
import RefreshRuntime from '/@react-refresh';RefreshRuntime.injectIntoGlobalHook(window);window.$RefreshReg$=()=>{};window.$RefreshSig$=()=>type=>type;window.__vite_plugin_react_preamble_installed__=true;
const React=(await import('/node_modules/.vite/deps/react.js')).default;
const {createRoot}=(await import('/node_modules/.vite/deps/react-dom_client.js')).default;
const {RenderableCodeBlock}=await import('/src/components/RenderableCodeBlock/RenderableCodeBlock.tsx');
const {ThemeProvider}=await import('/src/contexts/ThemeContext.tsx');
await import('/src/i18n.ts');
window.renderCase=(language,source)=>createRoot(document.getElementById('root')).render(React.createElement(ThemeProvider,null,React.createElement(RenderableCodeBlock,{block:true,lang:language},source)));
window.ready=true;
</script></body></html>`;
try {
  await page.route('**/__mainthread-ui',route=>route.fulfill({contentType:'text/html',body:html}));
  const cases=[['python','answer = 42\nprint(answer)'],['mermaid','graph TD; A[Start]-->B[Finish]'],['latex',String.raw`\int_0^1 x^2 dx`]];
  const results=[];
  for(const [language,source] of cases){
    await page.goto('http://localhost:5175/__mainthread-ui');
    await page.waitForFunction(()=>window.ready,{},{timeout:60000});
    await page.evaluate(([language,source])=>window.renderCase(language,source),[language,source]);
    if(language==='python')await page.waitForFunction(()=>document.querySelector('code span span')||document.querySelector('code span[style]'));
    else if(language==='mermaid')await page.locator('svg').filter({has:page.locator('g')}).first().waitFor({timeout:60000});
    else await page.locator('.katex').waitFor();
    await page.screenshot({path:path.join(root,'screenshots','component-'+language+'.png')});
    results.push({language,rendered:true,text:await page.locator('#root').innerText()});
  }
  fs.writeFileSync(path.join(root,'component-browser-check.json'),JSON.stringify({results,errors},null,2));
  if(errors.length)throw new Error(errors.join('\n'));
  console.log('Component browser checks passed');
} finally {await browser.close();}
