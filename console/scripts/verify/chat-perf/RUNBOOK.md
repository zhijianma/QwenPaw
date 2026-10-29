# 本轮真实浏览器测试

所有命令从 QwenPawQoder 根目录执行。禁止同时运行两个浏览器脚本。
脚本使用已安装的 Playwright 和 Google Chrome，不启动服务、不写测试消息。
产物固定放入 `console/scripts/verify/artifacts/chat-perf-2026-10-28/`。
重跑前应另选产物目录，避免覆盖已有样本；本次试跑记录已移入 calibration。

1. `node console/scripts/verify/chat-perf/verify-data.mjs`
   核验完整历史。默认复用本轮已下载原始数据，跨页内容按分片位置连续拼合。
   `data-consistency.json` 记录完整内容哈希；逐条比较 id/type/role/content/status，
   忽略随机传输 ID 和与展示内容无关的存储元数据。
2. `node console/scripts/verify/chat-perf/overhead.mjs`
   两端各三组开关对照，固定导航后 10 秒取主线程指标。
3. `node console/scripts/verify/chat-perf/negative-calibration.mjs`
   残留 DOM、缺失最新消息的负向检查。
4. `node console/scripts/verify/chat-perf/run-browser.mjs timing`
   两端四规模、四场景、三次。可追加端口和规模限制单组执行。
5. `node console/scripts/verify/chat-perf/scroll-browser.mjs`
   两次双向往返；逐检查点保存 ID、窗口、元素数、堆和网络请求。
6. `node console/scripts/verify/chat-perf/navigation-browser.mjs`
   折叠与展开分别执行目录定位、滚轮下行、反向、返回底部。
7. `node console/scripts/verify/chat-perf/trace-browser.mjs`
   独立追踪 100 与 1000 组，追踪结果不混入主计时。
8. `CHAT_PERF_HEADFUL=1 node console/scripts/verify/chat-perf/visible-check.mjs`
   可见 Chrome 窗口复核两端四组的加载、滚动与返回底部。
9. `node console/scripts/verify/chat-perf/summarize.mjs`
   导出原始 CSV 和分组统计。
10. 使用 qwenpaw-qoder 环境运行同目录 `plot_results.py` 生成曲线。

## 校准口径

- 1440×900 CSS 像素，DPR/缩放均为 1，浅色、非宽屏、显示思考、工具默认展示。
- 目标有效消息视口为 y=72..748；统计不包括输入框、标题和目录弹窗。
- 旧版容器 `.qwenpaw-bubble-list-scroll` 是 column-reverse，底部 scrollTop=0。
- 新版容器 `[data-history-message-window="true"]` 使用普通正向坐标。
- adapter 在业务读取 response.json 前记录当前会话响应 ID；从 React 气泡
  props 的 cards.output 建立聚合卡片 ID 映射，不以文字标记判断首屏可见。
- fetch 包装不重复解析响应、不修改返回数据、不延迟请求；开关对照两组均保留
  此 ID 适配器与轻量可见性计时，区别是 probe 的完整采样是否开启。
- 首屏稳定含 500ms 观察期；100ms 采样间隔不支持几十毫秒级确定性结论。
- 原 probe 的 injectionLate 是导航起点后 50ms 的启发式；浏览器初始化脚本
  可能晚于导航起点 50ms，但仍先于所有业务脚本，不能据该布尔值认定晚注入。
- 首屏稳定仅证明最新消息可见且位于底部，不代表全部旧历史已挂载。
- CDP TaskDuration 是主线程任务时间，不是系统 CPU 利用率；JS heap 为近似值。
- 页面进程 RSS 包括进程开销，不等于 JS heap。不同进程、GC 时机均影响比较。
- trace 的脚本、任务、布局等事件有嵌套，不能直接把全部事件耗时相加。

## 有效性与保留

试跑中的空辅助会话等待、侧栏选择器、路由查询串假设错误均保留在 calibration，
不纳入正式统计。正式计时没有滚动或展开；完成后才截图。
Console 的 React/Ant Design 弃用警告单独保留，不视为页面渲染异常。
任何超时、数据不一致、页面异常或采样截断不得混入成功中位数。

## 最终复核与输出

- 测试结束后执行 `node console/scripts/verify/chat-perf/verify-data.mjs --after`，重新读取完整历史到 calibration/after，不复用初始下载。
- 复现导航示例：`CHAT_PERF_HEADFUL=1 CHAT_PERF_SUFFIX=visible-repro node console/scripts/verify/chat-perf/navigation-browser.mjs 5175 50 result-collapsed`。已有同名文件会覆盖，重跑应使用新 suffix。当前脚本含最长 15 秒底部等待、边界等待及空白后 3 秒复核。
- `node console/scripts/verify/chat-perf/report.mjs` 在 summarize.mjs 后生成报告。
- 绘图用 qwenpaw-qoder Python，MPLCONFIGDIR 指向产物 calibration/matplotlib。
- 脚本引用本机已安装 Playwright 的绝对路径，产物日期目录也固定；迁移机器或另起一轮需修改这些路径，并保留原始记录。
- navigation/visible-check 的 complete 仅表示流程结束；功能判定还必须检查 errors、目标 ID、空白检查点、实际覆盖轮数和底部距离。
- 两端相同宽度偏好对应实际气泡宽度 928/818 CSS px；结果比较包含这一布局差异。
