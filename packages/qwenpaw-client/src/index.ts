import { createChatControlClient } from "./clients/chatControls.js";
import { createInteractionClient } from "./clients/interactions.js";
import { createRuntimeClient } from "./clients/runtime.js";
import { createTaskClient } from "./clients/tasks.js";
import {
  createFetchTransport,
  type FetchTransportOptions,
  type QwenPawTransport,
} from "./transport.js";

export function createQwenPawClient(
  options: FetchTransportOptions | QwenPawTransport,
) {
  const transport =
    "baseUrl" in options ? createFetchTransport(options) : options;
  return {
    runtime: createRuntimeClient(transport),
    chatControls: createChatControlClient(transport),
    tasks: createTaskClient(transport),
    interactions: createInteractionClient(transport),
  };
}

export type QwenPawClient = ReturnType<typeof createQwenPawClient>;

export * from "./clients/chatControls.js";
export * from "./clients/interactions.js";
export * from "./clients/runtime.js";
export * from "./clients/tasks.js";
export * from "./contracts/chatControls.js";
export * from "./contracts/interactions.js";
export * from "./contracts/runtime.js";
export * from "./contracts/tasks.js";
export * from "./transport.js";
