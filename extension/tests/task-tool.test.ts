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
  model?: string | null;
  access?: string;
  isolated?: boolean;
  maxRuntimeSeconds?: number;
};

type TaskResult = {
  content: Array<{ type: string; text: string }>;
  details: Record<string, unknown>;
  usage?: { totalTokens: number; cost: { total: number } };
};

type Renderable = {
  render: (width: number) => string[];
};

type TestTheme = {
  fg: (_color: string, text: string) => string;
  bold: (text: string) => string;
};

type RegisteredTool = {
  parameters: {
    properties: {
      model: { anyOf: Array<{ type: string }> };
    };
  };
  execute: (
    toolCallId: string,
    parameters: TaskParameters,
    signal: AbortSignal,
    onUpdate: ((result: TaskResult) => void) | undefined,
    context: { cwd: string; sessionManager: { getSessionId: () => string } },
  ) => Promise<TaskResult>;
  renderCall: (parameters: TaskParameters, theme: TestTheme, context: object) => Renderable;
  renderResult: (
    result: TaskResult,
    options: { expanded: boolean; isPartial: boolean },
    theme: TestTheme,
    context: { isError: boolean },
  ) => Renderable;
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
      type: "run.state.changed",
      agentId: "agt_test",
      runId: "run_test",
      data: { state: "running" },
    });
    onEvent({
      sequence: 6,
      timestamp: "now",
      type: "run.tool.started",
      agentId: "agt_test",
      runId: "run_test",
      data: { toolName: "bash" },
    });
    onEvent({
      sequence: 7,
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

const theme: TestTheme = {
  fg: (_color, text) => text,
  bold: (text) => text,
};

function render(component: Renderable): string {
  return component
    .render(120)
    .map((line) => line.trimEnd())
    .join("\n");
}

test("allows null to select the profile's default model", () => {
  const tool = registered(new FakeHubClient());

  assert.deepEqual(
    tool.parameters.properties.model.anyOf.map((schema) => schema.type),
    ["string", "null"],
  );
});

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
  assert.equal(updates.at(-1)?.content[0]?.text, "reviewer: running - bash\nworking");
  assert.equal(client.parameters?.cwd, "/repo");
  assert.equal(client.parameters?.rootSessionId, "root-session");
  assert.equal(client.parameters?.isolated, true);

  const call = tool.renderCall({ agent: "reviewer", prompt: "review", isolated: true }, theme, {});
  assert.match(render(call), /task reviewer \[isolated\]\n  review/);

  const partial = tool.renderResult(updates.at(-1)!, { expanded: false, isPartial: true }, theme, {
    isError: false,
  });
  const partialText = render(partial);
  assert.match(partialText, /Running · 0s · 1 tool call/);
  assert.match(partialText, /Current: bash/);
  assert.match(partialText, /Tools: bash/);
  assert.match(partialText, /Latest: working/);
  assert.doesNotMatch(partialText, /\[bash\]/);

  const completed = tool.renderResult(result, { expanded: false, isPartial: false }, theme, {
    isError: false,
  });
  assert.match(render(completed), /✓ Completed · 0s · 1 tool call\ndone/);
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
