/* Test-only adapter: retain response IDs before the application consumes JSON. */
(() => {
  window.perfHistory = new Map();
  const originalFetch = window.fetch;
  window.fetch = async (...args) => {
    const response = await originalFetch(...args);
    const url = new URL(response.url);
    const match = url.pathname.match(/^\/api\/chats\/([^/]+)(?:\/messages)?$/);
    if (match) {
      const json = response.json.bind(response);
      response.json = async () => {
        const data = await json();
        if (Array.isArray(data.messages)) {
          let record = window.perfHistory.get(match[1]);
          if (!record) window.perfHistory.set(match[1], record = {ids:new Set(),requests:0});
          for (const m of data.messages) record.ids.add(m.id);
          record.requests++;
          record.lastResponseCount = data.messages.length;
          if (!url.searchParams.has('before') && !url.searchParams.has('after_message_id')) {
            record.latestId = data.messages.at(-1)?.id;
            record.latestOriginalId = data.messages.at(-1)?.metadata?.original_id;
          }
        }
        return data;
      };
    }
    return response;
  };
  const cache = new WeakMap();
  window.resolvePerfTargets = (elements, config) => {
    const record = window.perfHistory.get(config.path.split('/').at(-1));
    if (!record) return;
    const expected=[];
    for (const el of elements) {
      if (record.ids.has(el.id)) expected.push(el.id);
      if (el.dataset.role !== 'assistant') continue;
      let ids=cache.get(el);
      if (!ids) {
        let fiber=el[Object.keys(el).find(k=>k.startsWith('__reactFiber'))];
        for(let i=0;fiber&&i<5;i++,fiber=fiber.return){
          const cards=fiber.memoizedProps?.cards;
          if(cards){ids=cards.flatMap(c=>(c.data?.output||[]).map(m=>m.id));break;}
        }
        if(ids?.length)cache.set(el,ids);
      }
      if(ids?.some(id=>record.ids.has(id)))expected.push(el.id);
      if(ids?.includes(record.latestId))config.latestMessageId=el.id;
    }
    config.expectedMessageIds=expected;
  };
})();
