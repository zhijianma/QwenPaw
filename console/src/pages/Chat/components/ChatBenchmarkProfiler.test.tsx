// @vitest-environment jsdom

import { render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ChatBenchmarkProfiler } from "./ChatBenchmarkProfiler";

describe("ChatBenchmarkProfiler", () => {
  afterEach(() => {
    delete window.__QWENPAW_CHAT_BENCHMARK__;
  });

  it("records commits only when browser benchmarking is enabled", async () => {
    window.__QWENPAW_CHAT_BENCHMARK__ = { commits: [] };
    const view = render(
      <ChatBenchmarkProfiler>
        <span>first</span>
      </ChatBenchmarkProfiler>,
    );
    view.rerender(
      <ChatBenchmarkProfiler>
        <span>second</span>
      </ChatBenchmarkProfiler>,
    );

    await waitFor(() => {
      expect(window.__QWENPAW_CHAT_BENCHMARK__?.commits.length).toBeGreaterThan(
        0,
      );
    });
  });

  it("does not create a benchmark target during normal rendering", () => {
    render(
      <ChatBenchmarkProfiler>
        <span>normal</span>
      </ChatBenchmarkProfiler>,
    );
    expect(window.__QWENPAW_CHAT_BENCHMARK__).toBeUndefined();
  });
});
