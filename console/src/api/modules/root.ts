import { request } from "../request";
import { createRuntimeClient } from "../../clients/runtimeClient";

const runtimeClient = createRuntimeClient({ request });

// Root API
export const rootApi = {
  readRoot: () => request<unknown>("/"),
  getVersion: runtimeClient.handshake,
};
