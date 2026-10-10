import { createServer } from "node:http";

const mode = process.env.FAKE_QWENPAW_MODE ?? "ready";

if (mode === "exit") process.exit(23);
if (mode === "no-launch") {
  setInterval(() => undefined, 1_000);
} else if (mode === "invalid-launch") {
  console.log("QWENPAW_MANAGED_HOST not-json");
  setInterval(() => undefined, 1_000);
} else {
  const server = createServer((request, response) => {
    if (request.url !== "/api/version") {
      response.writeHead(404).end();
      return;
    }
    response.setHeader("Content-Type", "application/json");
    response.end(
      mode === "invalid-handshake-value"
        ? JSON.stringify("wrong")
        : JSON.stringify(
            mode === "invalid-handshake"
              ? { schema: "wrong" }
              : {
                  schema: "qwenpaw.host-handshake.v1",
                  product: "qwenpaw",
                  version: "test",
                  protocol_version: 1,
                  features:
                    mode === "missing-feature" ? [] : ["chat.control.v1"],
                },
          ),
    );
    if (mode === "crash-after-handshake") {
      setTimeout(() => process.exit(31), 25);
    }
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
