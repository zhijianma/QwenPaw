import { Profiler, type ProfilerOnRenderCallback, type ReactNode } from "react";

export interface ChatBenchmarkCommit {
  phase: "mount" | "update" | "nested-update";
  actualDuration: number;
  baseDuration: number;
  startTime: number;
  commitTime: number;
}

interface ChatBenchmarkTarget {
  commits: ChatBenchmarkCommit[];
}

declare global {
  interface Window {
    __QWENPAW_CHAT_BENCHMARK__?: ChatBenchmarkTarget;
  }
}

const recordCommit: ProfilerOnRenderCallback = (
  _id,
  phase,
  actualDuration,
  baseDuration,
  startTime,
  commitTime,
) => {
  window.__QWENPAW_CHAT_BENCHMARK__?.commits.push({
    phase,
    actualDuration,
    baseDuration,
    startTime,
    commitTime,
  });
};

/** Enable React commit sampling only for an explicit browser benchmark. */
export function ChatBenchmarkProfiler({ children }: { children: ReactNode }) {
  if (!window.__QWENPAW_CHAT_BENCHMARK__) return children;
  return (
    <Profiler id="chat-timeline" onRender={recordCommit}>
      {children}
    </Profiler>
  );
}
