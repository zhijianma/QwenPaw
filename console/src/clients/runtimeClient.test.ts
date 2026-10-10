import { describe, expect, it, vi } from "vitest";

import {
  createRuntimeClient,
  missingRuntimeFeatures,
  RuntimeClientError,
} from "./runtimeClient";

const handshake = {
  schema: "qwenpaw.host-handshake.v1" as const,
  product: "qwenpaw" as const,
  version: "2.2.2b1",
  protocol_version: 1 as const,
  features: ["task.runtime", "future.feature"],
};

describe("runtime client", () => {
  it("accepts the current protocol and preserves future feature ids", async () => {
    const request = vi.fn().mockResolvedValue(handshake);

    await expect(createRuntimeClient({ request }).handshake()).resolves.toEqual(
      handshake,
    );
    expect(request).toHaveBeenCalledWith("/version");
  });

  it("rejects a legacy version-only response", async () => {
    const client = createRuntimeClient({
      request: vi.fn().mockResolvedValue({ version: "2.2.2b1" }),
    });

    await expect(client.handshake()).rejects.toMatchObject({
      code: "HOST_PROTOCOL_INCOMPATIBLE",
    } satisfies Partial<RuntimeClientError>);
  });

  it("reports only unavailable required features", () => {
    expect(
      missingRuntimeFeatures(handshake, ["task.runtime", "task.event-cursor"]),
    ).toEqual(["task.event-cursor"]);
  });
});
