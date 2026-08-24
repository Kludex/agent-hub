import assert from "node:assert/strict";
import test from "node:test";

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

import type { HubEvent, RunRecord } from "../src/client.js";
import { HubClient } from "../src/client.js";
import { registerTaskTool } from "../src/task-tool.js";

type TaskParameters = {
  agent: string;
  prompt: string;
  background?: boolean;
  model?: string;
  access?: string;
  isolated?: boolean;
  maxRuntimeSeconds?: number;
};

type TaskResult = {
  content: Array<{ type: string; text: string }>;
  details: Record<string, unknown>;
  usage?: { totalTokens: number; cost: { total: number } };
};

type RegisteredTool = {
  execute: (
    toolCallId: string,
    parameters: TaskParameters,
    signal: AbortSignal,
    onUpdate: ((result: TaskResult) => void) | undefined,
    context: { cwd: string; sessionManager: { getSessionId: () => string } },
  ) => Promise<TaskResult>;
};

class FakeHubClient extends HubClient {
  waited = false;
  parameters: Record<string, unknown> | undefined;

  constructor() {
    super("/tmp/not-used.sock");
  }

  override async ensureAvailable(): Promise<void> {}

  override async rpc<T>(method: string, parameters: Record<string, unknown> = {}): Promise<T> {
    if (method === "hub.snapshot") {
      return { latestSequence: 4 } as T;
    }
    this.parameters = parameters;
    return { agentId: "agt_test", runId: "run_test" } as T;
  }

  override async waitForRun(
    _runId: string,
    _after: number,
    onEvent: (event: HubEvent) => void,
  ): Promise<RunRecord> {
    this.waited = true;
    onEvent({
      sequence: 5,
      timestamp: "now",
      type: "run.output.delta",
      agentId: "agt_test",
      runId: "run_test",
      data: { text: "working" },
    });
    return {
      id: "run_test",
      agentId: "agt_test",
      state: "succeeded",
      prompt: "work",
      result: "done",
      usage: {
        tokens: { input: 2, output: 3, cacheRead: 1, cacheWrite: 0, total: 6 },
        cost: { total: 0.02 },
      },
    };
  }
}

function registered(client: FakeHubClient): RegisteredTool {
  let tool: RegisteredTool | undefined;
  const pi = {
    registerTool(value: unknown) {
      tool = value as RegisteredTool;
    },
  } as unknown as ExtensionAPI;
  registerTaskTool(pi, client);
  if (!tool) {
    throw new Error("Task tool was not registered");
  }
  return tool;
}

const context = {
  cwd: "/repo",
  sessionManager: { getSessionId: () => "root-session" },
};

test("returns blocking output, progress, and nested usage", async () => {
  const client = new FakeHubClient();
  const tool = registered(client);
  const updates: TaskResult[] = [];

  const result = await tool.execute(
    "call_test",
    { agent: "reviewer", prompt: "review", isolated: true },
    new AbortController().signal,
    (update) => updates.push(update),
    context,
  );

  assert.equal(result.content[0]?.text, "done");
  assert.equal(result.usage?.totalTokens, 6);
  assert.equal(result.usage?.cost.total, 0.02);
  assert.equal(updates[0]?.content[0]?.text, "working");
  assert.equal(client.parameters?.cwd, "/repo");
  assert.equal(client.parameters?.rootSessionId, "root-session");
  assert.equal(client.parameters?.isolated, true);
});

test("returns a background handle without waiting", async () => {
  const client = new FakeHubClient();
  const tool = registered(client);

  const result = await tool.execute(
    "call_background",
    { agent: "scout", prompt: "search", background: true },
    new AbortController().signal,
    undefined,
    context,
  );

  assert.match(result.content[0]?.text ?? "", /agt_test/);
  assert.equal(result.details.background, true);
  assert.equal(client.waited, false);
});
