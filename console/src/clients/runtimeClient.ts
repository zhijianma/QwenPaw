import type { RuntimeFeature, RuntimeHandshake } from "../contracts/runtime";

export interface RuntimeClientTransport {
  request<T>(path: string, init?: RequestInit): Promise<T>;
}

export class RuntimeClientError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = "RuntimeClientError";
    this.code = code;
  }
}

function parseRuntimeHandshake(value: unknown): RuntimeHandshake {
  if (typeof value !== "object" || value === null) {
    throw new RuntimeClientError(
      "HOST_HANDSHAKE_INVALID",
      "QwenPaw Host returned an invalid handshake",
    );
  }
  const candidate = value as Partial<RuntimeHandshake>;
  if (
    candidate.schema !== "qwenpaw.host-handshake.v1" ||
    candidate.product !== "qwenpaw" ||
    candidate.protocol_version !== 1 ||
    typeof candidate.version !== "string" ||
    !Array.isArray(candidate.features) ||
    !candidate.features.every((feature) => typeof feature === "string")
  ) {
    throw new RuntimeClientError(
      "HOST_PROTOCOL_INCOMPATIBLE",
      "QwenPaw Host does not support protocol qwenpaw.host-handshake.v1",
    );
  }
  return candidate as RuntimeHandshake;
}

export function missingRuntimeFeatures(
  handshake: RuntimeHandshake,
  required: readonly RuntimeFeature[],
): RuntimeFeature[] {
  const available = new Set(handshake.features);
  return required.filter((feature) => !available.has(feature));
}

export function createRuntimeClient(transport: RuntimeClientTransport) {
  return {
    async handshake(signal?: AbortSignal) {
      const value = signal
        ? await transport.request<unknown>("/version", { signal })
        : await transport.request<unknown>("/version");
      return parseRuntimeHandshake(value);
    },
  };
}

export type RuntimeClient = ReturnType<typeof createRuntimeClient>;
