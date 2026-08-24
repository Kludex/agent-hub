import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { createServer } from "node:http";
import { mkdir, rm } from "node:fs/promises";
import test from "node:test";

import type { HubEvent, RunRecord } from "../src/client.js";
import { HubClient } from "../src/client.js";

test("uses JSON-RPC and follows the resumable event stream", async () => {
  const directory = `/tmp/agent-hub-extension-${randomUUID()}`;
  const socketPath = `${directory}/hub.sock`;
  await mkdir(directory, { mode: 0o700 });
  let runState: RunRecord["state"] = "running";
  const eventCursors: string[] = [];
  let snapshotCalls = 0;
  const server = createServer((request, response) => {
    if (request.url === "/health") {
      response.writeHead(200, { "content-type": "application/json" });
      response.end('{"status":"ok"}');
      return;
    }
    if (request.url?.startsWith("/v1/events")) {
      eventCursors.push(request.url);
      response.writeHead(200, { "content-type": "application/x-ndjson" });
      const reconnecting = eventCursors.length > 1;
      const notification = `${JSON.stringify({
        jsonrpc: "2.0",
        method: "agent.event",
        params: {
          sequence: reconnecting ? 9 : 4,
          timestamp: "now",
          type: reconnecting ? "run.state.changed" : "run.output.delta",
          agentId: "agt_test",
          runId: "run_test",
          data: reconnecting ? { state: "succeeded" } : { text: "partial" },
        },
      })}\n`;
      if (reconnecting) {
        runState = "succeeded";
      }
      response.write(notification.slice(0, 17));
      response.end(notification.slice(17));
      return;
    }
    const chunks: Buffer[] = [];
    request.on("data", (chunk: Buffer) => chunks.push(chunk));
    request.on("end", () => {
      const command = JSON.parse(Buffer.concat(chunks).toString()) as {
        id: string;
        method: string;
      };
      if (command.method === "hub.snapshot") {
        snapshotCalls += 1;
      }
      const result =
        command.method === "hub.snapshot"
          ? { latestSequence: eventCursors.length > 1 ? 9 : 3 }
          : {
              run: {
                id: "run_test",
                agentId: "agt_test",
                state: runState,
                prompt: "test",
                result: runState === "succeeded" ? "done" : undefined,
                usage: {},
              },
            };
      response.writeHead(200, { "content-type": "application/x-ndjson" });
      response.end(`${JSON.stringify({ jsonrpc: "2.0", id: command.id, result })}\n`);
    });
  });
  await new Promise<void>((resolve) => server.listen(socketPath, resolve));
  const client = new HubClient(socketPath);
  const events: HubEvent[] = [];

  try {
    await client.health();
    assert.deepEqual(await client.rpc("hub.snapshot"), { latestSequence: 3 });
    const run = await client.waitForRun("run_test", 3, (event) => events.push(event));
    assert.equal(run.result, "done");
    assert.deepEqual(
      events.map((event) => event.sequence),
      [4],
    );
    assert.deepEqual(eventCursors, ["/v1/events?after=3", "/v1/events?after=4"]);
    assert.equal(snapshotCalls, 2);
  } finally {
    await new Promise<void>((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    );
    await rm(directory, { recursive: true, force: true });
  }
});
