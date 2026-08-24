import assert from "node:assert/strict";
import test from "node:test";

import type { ExtensionCommandContext } from "@earendil-works/pi-coding-agent";

import type { AgentRecord, RunRecord } from "../src/client.js";
import { HubClient } from "../src/client.js";
import { showHub } from "../src/hub.js";

const agent: AgentRecord = {
  id: "agt_test",
  runtime: "pi",
  profile: "reviewer",
  cwd: "/repo",
  isolated: false,
  detached: false,
  state: "running",
  createdAt: "2026-08-24T00:00:00Z",
  updatedAt: "2026-08-24T00:00:00Z",
};

const run: RunRecord = {
  id: "run_test",
  agentId: agent.id,
  state: "running",
  prompt: "Review the changes",
  startedAt: new Date().toISOString(),
  usage: { totalTokens: 12, cost: 0.01 },
};

class HubFixture extends HubClient {
  snapshots = 0;
  calls: string[] = [];

  constructor() {
    super("/tmp/not-used.sock");
  }

  override async ensureAvailable(): Promise<void> {}

  override async rpc<T>(method: string): Promise<T> {
    this.calls.push(method);
    if (method === "hub.snapshot") {
      this.snapshots += 1;
      return (
        this.snapshots === 1
          ? { agents: [agent], activeRuns: [run], latestSequence: 1 }
          : { agents: [], activeRuns: [], latestSequence: 1 }
      ) as T;
    }
    if (method === "agent.get") {
      return { agent, runs: [run], events: [] } as T;
    }
    return { aborted: true } as T;
  }
}

test("renders a snapshot and dispatches an agent action", async () => {
  const client = new HubFixture();
  const notifications: string[] = [];
  const context = {
    ui: {
      async select(title: string, options: string[]) {
        return title === "Agent Hub" ? options[0] : "Abort";
      },
      notify(message: string) {
        notifications.push(message);
      },
    },
  } as unknown as ExtensionCommandContext;

  await showHub(context, client);

  assert.deepEqual(client.calls, ["hub.snapshot", "agent.get", "agent.abort", "hub.snapshot"]);
  assert.deepEqual(notifications, ["Agent Hub has no agents."]);
});
