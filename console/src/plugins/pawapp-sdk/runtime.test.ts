import { beforeEach, describe, expect, it, vi } from "vitest";

import { hostFetch } from "../hostSdk/fetch";
import { createHostRuntimeNamespace } from "./runtime";

vi.mock("../hostSdk/fetch", () => ({ hostFetch: vi.fn() }));

const mockedHostFetch = vi.mocked(hostFetch);
const handshake = {
  schema: "qwenpaw.host-handshake.v1",
  product: "qwenpaw",
  version: "2.2.2b1",
  protocol_version: 1,
  features: ["task.runtime", "task.event-cursor"],
};

describe("PawApp Host runtime", () => {
  beforeEach(() => mockedHostFetch.mockReset());

  it("caches negotiation and exposes feature checks", async () => {
    mockedHostFetch.mockResolvedValue(
      new Response(JSON.stringify(handshake), {
        headers: { "Content-Type": "application/json" },
      }),
    );
    const runtime = createHostRuntimeNamespace();

    await expect(runtime.supports("task.runtime")).resolves.toBe(true);
    await expect(runtime.supports("artifact.references")).resolves.toBe(false);
    expect(mockedHostFetch).toHaveBeenCalledTimes(1);
  });

  it("fails closed when required features are missing", async () => {
    mockedHostFetch.mockResolvedValue(
      new Response(JSON.stringify(handshake), {
        headers: { "Content-Type": "application/json" },
      }),
    );

    await expect(
      createHostRuntimeNamespace().require(["artifact.references"]),
    ).rejects.toMatchObject({ code: "HOST_FEATURES_UNAVAILABLE" });
  });

  it("maps an incompatible handshake to a PawApp runtime error", async () => {
    mockedHostFetch.mockResolvedValue(
      new Response(JSON.stringify({ version: "2.1.0" }), {
        headers: { "Content-Type": "application/json" },
      }),
    );

    await expect(
      createHostRuntimeNamespace().handshake({
        signal: new AbortController().signal,
      }),
    ).rejects.toMatchObject({ code: "HOST_PROTOCOL_INCOMPATIBLE" });
  });
});
