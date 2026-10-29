(() => {
 const data={firstHistoryMs:null,firstDom:null,longTasks:[],requests:[],scrolls:[],frames:[]};
 new PerformanceObserver(l=>{for(const e of l.getEntries())data.longTasks.push({start:e.startTime,ms:e.duration});}).observe({type:'longtask',buffered:true});
 new PerformanceObserver(l=>{for(const e of l.getEntries())if(/localhost:800[13]\/api\//.test(e.name))data.requests.push({url:e.name,start:e.startTime,ms:e.duration,bytes:e.transferSize});}).observe({type:'resource',buffered:true});
 let last=performance.now(),published=0,until=0;
 document.addEventListener('scroll',e=>{
  const el=e.target;
  if(el instanceof HTMLElement && (String(el.className).includes('HistoryMessageWindow-module__list') || el.classList.contains('qwenpaw-bubble-list-scroll'))){
   until=performance.now()+500;
   data.scrolls.push({time:performance.now(),top:el.scrollTop,height:el.scrollHeight});
  }
 },true);
 function frame(now){
  if(data.firstHistoryMs===null){
   const list=document.querySelector('[class*="HistoryMessageWindow-module__list"]')||document.querySelector('.qwenpaw-bubble-list-scroll');
   if(list && (list.textContent.includes('[History Performance') || list.textContent.includes('[ASSISTANT BLOCK'))){
    data.firstHistoryMs=now;data.firstDom=document.querySelectorAll('*').length;
   }
  }
  if(now-last>50 && now<until)data.frames.push({time:now,ms:now-last});
  if(now-published>500){data.dom=document.querySelectorAll('*').length;document.documentElement.dataset.scalePerf=JSON.stringify(data);published=now;}
  last=now;requestAnimationFrame(frame);
 }
 requestAnimationFrame(frame);
})();
