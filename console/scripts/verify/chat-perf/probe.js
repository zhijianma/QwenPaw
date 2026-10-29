/* Browser-only probe. Inject before navigation; never import into the app. */
(() => {
  if (window.chatPerf) return;
  const installedAt = performance.now();
  const observers = [];
  const samples = [];
  let active = null;
  let timer = null;
  const defaultScroller = '[data-history-message-window="true"], .qwenpaw-bubble-list-scroll, [class*="chat-anywhere-message-list-bubble-scroll"]';
  const elapsed = () => performance.now() - active.start;
  function record(kind, entry) {
    if (!active || entry.startTime < active.start) return;
    const list = active[kind];
    if (list.some(item => item.name === entry.name && item.startTime === entry.startTime)) return;
    if (list.length < 10000) list.push(entry.toJSON());
    else active.truncated[kind] = true;
  }
  for (const [type, kind] of [['resource', 'resources'], ['longtask', 'longTasks']]) {
    if (!PerformanceObserver.supportedEntryTypes.includes(type)) continue;
    const observer = new PerformanceObserver(list => {
      for (const entry of list.getEntries()) record(kind, entry);
    });
    observer.observe({ type, buffered: true });
    observers.push([observer, kind]);
  }
  function nodes() {
    const list = document.querySelector(active.config.scroller || defaultScroller);
    return { list, messages: list ? [...list.querySelectorAll('[data-role][id]')] : [] };
  }
  function visible(element, list) {
    const r = element.getBoundingClientRect();
    const s = list.getBoundingClientRect();
    const top = Math.max(s.top, active.config.viewportTop || 0);
    const bottom = Math.min(s.bottom, active.config.viewportBottom || innerHeight);
    return r.width > 0 && r.height > 0 && r.bottom > top && r.top < bottom &&
      r.right > 0 && r.left < innerWidth && getComputedStyle(element).visibility !== 'hidden';
  }
  function tick() {
    if (!active || document.visibilityState !== 'visible') return;
    if (location.pathname !== active.config.path) return;
    const { list, messages } = nodes();
    if (!list) return;
    active.config.resolveTargets?.(messages, active.config);
    const seen = messages.filter(el => visible(el, list) &&
      active.config.expectedMessageIds?.includes(el.id));
    if (active.firstVisibleMs === null && seen.length) {
      active.firstVisibleMs = elapsed();
      active.firstVisibleIds = seen.map(el => el.id);
    }
    // Require an independently verified final message ID: DOM tail alone is
    // not proof that the latest history page has been loaded.
    const target = messages.find(el => el.id === active.config.latestMessageId);
    const ready = target && visible(target, list) &&
      list.dataset.historyLoading !== 'true' &&
      (active.config.reverseScroll
        ? Math.abs(list.scrollTop) <= 24
        : list.scrollHeight - list.clientHeight - list.scrollTop <= 24);
    if (!ready) { active.stableSince = null; active.geometry = null; return; }
    if (active.latestVisibleMs === null) active.latestVisibleMs = elapsed();
    const r = target.getBoundingClientRect();
    const geometry = [r.top, r.bottom, list.scrollTop, list.scrollHeight, list.clientHeight];
    const unchanged = active.geometry && geometry.every((v, i) => Math.abs(v - active.geometry[i]) <= 1);
    if (!unchanged) active.stableSince = performance.now();
    active.geometry = geometry;
    if (active.firstStableMs === null && performance.now() - active.stableSince >= 500) {
      active.firstStableMs = elapsed();
      checkpoint('first-stable');
    }
  }
  function checkpoint(label) {
    if (!active) throw new Error('Call chatPerf.start first');
    const started = performance.now();
    const { list, messages } = nodes();
    const windows = list ? [...list.querySelectorAll('[data-measured-message-window]')].map(root => ({
      nested: Boolean(root.parentElement?.closest('[data-measured-message-window]')),
      rowIds: [...root.children].filter(el => el.hasAttribute('data-window-row')).map(el => el.getAttribute('data-window-row')),
    })) : [];
    const point = {
      label, timeMs: elapsed(), path: location.pathname,
      documentElements: document.querySelectorAll('*').length,
      messageElements: list ? list.querySelectorAll('*').length : null,
      messages: messages.map(el => ({ id: el.id, role: el.dataset.role })),
      visibleIds: list ? messages.filter(el => visible(el, list)).map(el => el.id) : [],
      windows,
      scroll: list ? { top: list.scrollTop, height: list.scrollHeight, clientHeight: list.clientHeight } : null,
      // Chromium-specific, approximate; not renderer process memory.
      jsHeapBytes: performance.memory?.usedJSHeapSize ?? null,
      cpu: null, processMemoryBytes: null,
    };
    point.probeCostMs = performance.now() - started;
    active.checkpoints.push(point);
    return point;
  }
  function stop(status = 'complete') {
    if (!active) return null;
    clearInterval(timer);
    for (const [observer, kind] of observers) {
      for (const entry of observer.takeRecords()) record(kind, entry);
    }
    active.endMs = elapsed();
    active.status = status;
    active.latestVerified = active.firstStableMs !== null;
    const { geometry, stableSince, ...result } = active;
    samples.push(result);
    active = null;
    return result;
  }
  window.chatPerf = {
    start(config) {
      if (active) throw new Error('Stop the previous sample first');
      if (!config.sampleId || !config.path || !config.scenario) throw new Error('sampleId, path and scenario required');
      const start = config.startTime ?? performance.now();
      active = {
        config, start, installedAt, timeOrigin: performance.timeOrigin,
        injectionLate: installedAt > start + 50,
        viewport: { width: innerWidth, height: innerHeight, dpr: devicePixelRatio },
        firstVisibleMs: null, latestVisibleMs: null, firstStableMs: null,
        stableSince: null, geometry: null, resources: [], longTasks: [],
        errors: [], checkpoints: [], truncated: {},
      };
      // Entries may have been delivered before start() was called.
      for (const type of ['resource', 'longtask']) {
        for (const entry of performance.getEntriesByType(type)) record(type === 'resource' ? 'resources' : 'longTasks', entry);
      }
      timer = setInterval(tick, 100);
    },
    checkpoint, stop,
    peek() { return active ? { firstVisibleMs: active.firstVisibleMs,
      latestVisibleMs: active.latestVisibleMs, firstStableMs: active.firstStableMs } : null; },
    export() { return { version: 1, samples }; },
    dispose() {
      stop('disposed');
      for (const [observer] of observers) observer.disconnect();
      window.removeEventListener('error', onError);
      window.removeEventListener('unhandledrejection', onRejection);
      delete window.chatPerf;
    },
  };
  function error(message) {
    if (active && active.errors.length < 100) active.errors.push({ timeMs: elapsed(), message });
  }
  function onError(event) { error(event.message); }
  function onRejection(event) { error(String(event.reason)); }
  window.addEventListener('error', onError);
  window.addEventListener('unhandledrejection', onRejection);
})();
