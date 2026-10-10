import { createServer } from "node:http";

const mode = process.env.FAKE_QWENPAW_MODE ?? "ready";
const runtimeProjection = {
  schema: "qwenpaw.conversation-runtime-projection.v1",
  agent_id: "agent-1",
  chat_id: "chat-1",
  queue: {
    schema: "qwenpaw.queue-projection.v1",
    chat_id: "chat-1",
    revision: 2,
    submissions: [],
  },
  interactions: [],
  execution_chains: [],
  activity: {
    schema: "qwenpaw.observation-page.v1",
    items: [],
    next_cursor: null,
  },
  cursor: "cursor-2",
  observed_at: "2026-10-10T00:00:00Z",
};

function sendJson(response, status, body) {
  response.writeHead(status, { "Content-Type": "application/json" });
  response.end(JSON.stringify(body));
}

if (mode === "exit") process.exit(23);
if (mode === "no-launch") {
  setInterval(() => undefined, 1_000);
} else if (mode === "invalid-launch") {
  console.log("QWENPAW_MANAGED_HOST not-json");
  setInterval(() => undefined, 1_000);
} else {
  const server = createServer((request, response) => {
    const url = new URL(request.url ?? "/", "http://127.0.0.1");
    if (url.pathname === "/api/version") {
      sendJson(
        response,
        200,
        mode === "invalid-handshake-value"
          ? "wrong"
          : mode === "invalid-handshake"
            ? { schema: "wrong" }
            : {
                schema: "qwenpaw.host-handshake.v1",
                product: "qwenpaw",
                version: "test",
                protocol_version: 1,
                features:
                  mode === "missing-feature" ? [] : ["chat.control.v1"],
              },
      );
      if (mode === "crash-after-handshake") {
        setTimeout(() => process.exit(31), 25);
      }
      return;
    }
    if (url.pathname === "/api/chats/chat-1") {
      sendJson(response, 200, {
        messages: [{ id: "message-1", role: "assistant", content: "ready" }],
        status: "idle",
      });
      return;
    }
    if (url.pathname === "/api/chats/chat-1/runtime") {
      sendJson(response, 200, runtimeProjection);
      return;
    }
    if (url.pathname === "/api/chats/chat-1/runtime/stream") {
      if (request.headers["last-event-id"] !== "cursor-1") {
        sendJson(response, 400, { detail: "missing runtime cursor" });
        return;
      }
      response.writeHead(200, {
        "Cache-Control": "no-cache",
        "Content-Type": "text/event-stream",
      });
      response.end(
        `id: cursor-2\nevent: snapshot\ndata: ${JSON.stringify(
          runtimeProjection,
        )}\n\n`,
      );
      return;
    }
    if (url.pathname === "/api/chats/missing") {
      sendJson(response, 404, { detail: "chat missing" });
      return;
    }
    response.writeHead(404).end();
  });

  server.listen(0, "127.0.0.1", () => {
    const address = server.address();
    if (typeof address === "string" || address === null) process.exit(24);
    console.log(
      `QWENPAW_MANAGED_HOST ${JSON.stringify({
        schema: "qwenpaw.managed-host-launch.v1",
        api_url:
          mode === "invalid-api-root"
            ? `http://127.0.0.1:${address.port}/other/api`
            : `http://127.0.0.1:${address.port}/api`,
        pid: process.pid,
      })}`,
    );
  });

  process.on("SIGTERM", () => {
    if (mode !== "ignore-term") server.close(() => process.exit(0));
  });
}
