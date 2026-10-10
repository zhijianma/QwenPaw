import {
  createRuntimeClient,
  missingRuntimeFeatures,
  RuntimeClientError,
} from "../../clients/runtimeClient";
import { hostFetch } from "../hostSdk/fetch";
import type {
  PawHostHandshake,
  PawHostRuntimeNamespace,
  PawRuntimeFeature,
} from "./types";

export class PawHostRuntimeError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = "PawHostRuntimeError";
    this.code = code;
  }
}

async function readJson<T>(response: Response): Promise<T> {
  if (response.ok) return response.json() as Promise<T>;
  const text = await response.text().catch(() => "");
  throw new PawHostRuntimeError(
    `HTTP_${response.status}`,
    text || response.statusText || "Host handshake failed",
  );
}

export function createHostRuntimeNamespace(): PawHostRuntimeNamespace {
  const client = createRuntimeClient({
    request: async <T>(path: string, init?: RequestInit) =>
      readJson<T>(await hostFetch(path, init)),
  });
  let cached: Promise<PawHostHandshake> | undefined;

  async function negotiate(signal?: AbortSignal): Promise<PawHostHandshake> {
    try {
      return await client.handshake(signal);
    } catch (error) {
      if (error instanceof RuntimeClientError) {
        throw new PawHostRuntimeError(error.code, error.message);
      }
      throw error;
    }
  }

  async function handshake(options: { signal?: AbortSignal } = {}) {
    if (options.signal) return negotiate(options.signal);
    if (!cached) {
      cached = negotiate().catch((error: unknown) => {
        cached = undefined;
        throw error;
      });
    }
    return cached;
  }

  async function requireFeatures(
    features: readonly PawRuntimeFeature[],
    options: { signal?: AbortSignal } = {},
  ): Promise<PawHostHandshake> {
    const current = await handshake(options);
    const missing = missingRuntimeFeatures(current, features);
    if (missing.length > 0) {
      throw new PawHostRuntimeError(
        "HOST_FEATURES_UNAVAILABLE",
        `QwenPaw Host is missing required features: ${missing.join(", ")}`,
      );
    }
    return current;
  }

  return {
    handshake,
    async supports(feature, options) {
      const current = await handshake(options);
      return current.features.includes(feature);
    },
    require: requireFeatures,
  };
}
