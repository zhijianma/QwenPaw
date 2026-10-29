Chat performance probe — browser injection, no service changes

Checklist
[x] Isolated timing per sample
[x] Expected message IDs and visible geometry, not textContent markers
[x] Latest message + bottom position + 500ms stable geometry
[x] Separate document/message DOM, message IDs, nested window row IDs
[x] Resource timing, long tasks, errors, checkpoint overhead
[x] JSON export and observer cleanup
[x] Syntax and DOM smoke verification
[ ] Real browser calibration on 5173 and 5175 by the test executor
[ ] CPU/process memory/trace collected externally

Do not use ../scale-instrument.js for the new comparison.
Never import probe.js into the application or edit either index.html.
Reusable scripts and fixtures are committed on the test-refactor-chat branch.
Do not commit artifacts, intermediate measurements, or analysis reports.
Do not start or restart services.

Injection (example for an executor with Playwright/CDP capability)
  await context.addInitScript({ path: absolutePathToProbe });
  await context.addInitScript(config => {
    window.chatPerf.start(config);
  }, config);
  await page.goto(url);

Use a fresh context for each navigation sample so init scripts do not accumulate.
The two scripts must run in the stated order; preferably concatenate probe.js
and the start call into one init script when the browser tool does not guarantee
init-script order. An equivalent CDP addScriptToEvaluateOnNewDocument works.
Late console injection cannot measure initial load and must not be used for it.

Required configuration example
  {
    sampleId: '5175-100-cold-1',
    scenario: 'cold', // cold | reload | first-switch | cached-return
    path: '/chat/6f0cb439-2b27-467d-84b1-119a74b05641',
    startTime: 0, // navigation time origin; OMIT for a sidebar switch
    latestMessageId: 'verified-final-message-id',
    expectedMessageIds: ['verified-user-id', 'verified-assistant-id'],
    viewportTop: 80,
    viewportBottom: 650
  }

Obtain expected IDs from verified fixture/API data before timing. They must
match actual data-role/id DOM IDs on each version. If IDs are transformed,
record and verify that mapping. Do not infer completeness from the last DOM
element. expectedMessageIds should cover the initial page; unknown IDs mean
the visibility timings remain null, not success. Do not compare those samples.
viewportTop/Bottom are measured unobscured message viewport bounds excluding
header/composer overlays, in CSS pixels. Calibrate separately for each version
with the same viewport. Defaults do not account for overlays.
Override config.scroller only after verifying the real scroll container.

Sidebar switching
  await page.evaluate(config => window.chatPerf.start(config), config);
  // Perform the real sidebar click immediately afterward.
  // startTime omitted: timing begins just before click dispatch.
  // Record command-dispatch delay if controller latency is material.
  // Stop the prior sample before starting the next one.
The expected IDs prevent old conversation DOM from satisfying first-visible.
Do not reuse navigation startTime=0 for a switch.

Checkpoints and export
  await page.evaluate(() => window.chatPerf.checkpoint('page-01-settled'));
  await page.evaluate(() => window.chatPerf.stop('complete'));
  const result = await page.evaluate(() => window.chatPerf.export());
Write result as JSON outside the page. Use stop('timeout') for incomplete runs.
Only one active sample is permitted. Start a separate scenario='scroll' sample
for sustained scrolling; capture checkpoints after each page settles and at
direction reversal. Compare message IDs and window row IDs to show eviction.
Do not use full DOM scans per frame. Checkpoints record their own probeCostMs.

Interpretation and limitations
- Timing resolution is 100ms; firstStableMs includes the 500ms stability check.
  Report that convention, do not call it exact paint time. Validate with filmstrip.
- firstVisibleMs requires a verified expected message intersecting the viewport.
- latestVerified requires the known latest message, bottom <=24px, no declared
  history-loading state and stable geometry. It does not prove all history loaded.
- DOM metrics count elements, not text/comment nodes or CDP total DOM nodes.
- CPU and renderer memory are null: collect externally with CDP/Chrome trace or
  process metrics. Long tasks are NOT CPU utilization.
- jsHeapBytes is Chromium's optional approximate performance.memory metric;
  unsupported environments report null. It is not process memory.
- Resource entries retain all URLs, including same-origin proxies. Classify
  history requests afterward. Cross-origin transfer sizes may be restricted;
  transferSize=0 alone does not prove a cache hit. Use network/CDP evidence.
- Arrays cap at 10000 entries per kind, errors at 100; truncated flags invalidate
  complete event-count claims. Export/stop between bounded sampling intervals.
- Do not run two test pages simultaneously. Keep display/collapse state equal.
- Cold browser cache is not cold server startup. Prepare databases beforehand.
- Run a probe-on/off control to quantify measurement overhead before comparison.
- No new performance result has been produced by installing this probe.
