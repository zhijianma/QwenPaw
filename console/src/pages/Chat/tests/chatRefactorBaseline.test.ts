/** Machine-readable baseline for the legacy full-history frontend path. */
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import { performance } from "node:perf_hooks";
import { describe, expect, it, vi } from "vitest";
import type { Message } from "../../../api/types/chat";

vi.mock("../../../api/modules/chat", async (importOriginal) => {
  const actual = await importOriginal<
    typeof import("../../../api/modules/chat")
  >();
  return {
    ...actual,
    chatApi: {
      ...actual.chatApi,
      filePreviewUrl: vi.fn(
        (path: string) => `http://localhost:8000/files/preview/${path}`,
      ),
    },
  };
});

import { __test__ } from "../sessionApi";

const { convertMessages } = __test__;
const MIB = 1024 * 1024;

type Scenario = {
  name: string;
  targetBytes: number;
  turns: number;
  messagesPerTurn: number;
};

type Sample = {
  index: number;
  parseMs: number;
  convertMs: number;
  totalMs: number;
  heapBefore: number;
  heapAfter: number;
};

const scenarios: Scenario[] = [
  {
    name: "typical",
    targetBytes: 5 * MIB,
    turns: 100,
    messagesPerTurn: 5,
  },
  {
    name: "long",
    targetBytes: 100 * MIB,
    turns: 2_000,
    messagesPerTurn: 5,
  },
];

function sizedText(prefix: string, size: number): string {
  if (prefix.length >= size) return prefix.slice(0, size);
  return `${prefix}${"x".repeat(size - prefix.length)}`;
}

function buildMessages(scenario: Scenario): Message[] {
  const count = scenario.turns * scenario.messagesPerTurn;
  const perMessage = Math.floor(scenario.targetBytes / count);
  const remainder = scenario.targetBytes % count;
  const messages: Message[] = [];
  let index = 0;
  for (let turn = 0; turn < scenario.turns; turn += 1) {
    for (let ordinal = 0; ordinal < scenario.messagesPerTurn; ordinal += 1) {
      const size = perMessage + (index < remainder ? 1 : 0);
      messages.push({
        id: `${scenario.name}-${turn}-${ordinal}`,
        role: ordinal === 0 ? "user" : "assistant",
        content: [
          {
            type: "text",
            text: sizedText(`${scenario.name}:${turn}:${ordinal}:`, size),
          },
        ],
        metadata: {
          timestamp: "2026-01-01T00:00:00.000Z",
        },
      });
      index += 1;
    }
  }
  return messages;
}

function percentile(values: number[], fraction: number): number {
  const sorted = [...values].sort((left, right) => left - right);
  const index = Math.min(
    sorted.length - 1,
    Math.ceil(sorted.length * fraction) - 1,
  );
  return sorted[index];
}

function summarize(samples: Sample[]) {
  const totals = samples.map((sample) => sample.totalMs);
  return {
    count: totals.length,
    p50Ms: percentile(totals, 0.5),
    p95Ms: percentile(totals, 0.95),
    maxMs: Math.max(...totals),
  };
}

const baselineEnabled = process.env.CHAT_REFACTOR_BASELINE === "1";
const baselineDescribe = baselineEnabled ? describe : describe.skip;

baselineDescribe("Chat refactor legacy frontend baseline", () => {
  it("records full-history parse and conversion samples", () => {
    const sampleCount = Number(
      process.env.CHAT_REFACTOR_BASELINE_SAMPLES ?? "30",
    );
    const outputPath = process.env.CHAT_REFACTOR_BASELINE_OUT;
    expect(outputPath, "CHAT_REFACTOR_BASELINE_OUT is required").toBeTruthy();
    expect(sampleCount).toBeGreaterThanOrEqual(1);

    const results = scenarios.map((scenario) => {
      const serialized = JSON.stringify(buildMessages(scenario));
      const expectedCount = scenario.turns * scenario.messagesPerTurn;
      const samples: Sample[] = [];

      for (let warmup = 0; warmup < 3; warmup += 1) {
        convertMessages(JSON.parse(serialized) as Message[]);
      }
      for (let index = 0; index < sampleCount; index += 1) {
        const heapBefore = process.memoryUsage().heapUsed;
        const started = performance.now();
        const messages = JSON.parse(serialized) as Message[];
        const parsed = performance.now();
        const converted = convertMessages(messages);
        const finished = performance.now();
        expect(messages).toHaveLength(expectedCount);
        expect(converted.length).toBeGreaterThan(0);
        samples.push({
          index,
          parseMs: parsed - started,
          convertMs: finished - parsed,
          totalMs: finished - started,
          heapBefore,
          heapAfter: process.memoryUsage().heapUsed,
        });
      }
      return {
        scenario,
        serializedBytes: Buffer.byteLength(serialized),
        samples,
        summary: summarize(samples),
      };
    });

    const payload = {
      schemaVersion: 1,
      recordedAt: new Date().toISOString(),
      runtime: {
        node: process.version,
        platform: process.platform,
        arch: process.arch,
      },
      gitCommit: process.env.CHAT_REFACTOR_GIT_COMMIT ?? "unknown",
      cacheState: "warm-conversion-after-three-warmups",
      results,
    };
    mkdirSync(dirname(outputPath!), { recursive: true });
    writeFileSync(outputPath!, JSON.stringify(payload, null, 2));
  });
});
