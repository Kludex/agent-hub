import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

import type { HubEvent, RunRecord } from "./client.js";
import { HubClient } from "./client.js";

type Usage = {
  input: number;
  output: number;
  cacheRead: number;
  cacheWrite: number;
  totalTokens: number;
  cost: {
    input: number;
    output: number;
    cacheRead: number;
    cacheWrite: number;
    total: number;
  };
};

export function registerTaskTool(pi: ExtensionAPI, client: HubClient): void {
  pi.registerTool({
    name: "task",
    label: "Task",
    description: "Delegate a task to an Agent Hub profile. Results are limited to 50KB.",
    promptSnippet: "Delegate focused coding, exploration, or review work to an Agent Hub profile",
    promptGuidelines: [
      "Use task when independent repository work can run concurrently or benefit from a specialized agent profile.",
    ],
    parameters: Type.Object({
      agent: Type.String({ description: "Reusable Agent Hub profile name" }),
      prompt: Type.String({ description: "Complete task for the delegated agent" }),
      background: Type.Optional(Type.Boolean({ default: false })),
      model: Type.Optional(Type.String()),
      access: Type.Optional(Type.String({ description: "read-only or shared-write" })),
      isolated: Type.Optional(Type.Boolean({ default: false })),
      maxRuntimeSeconds: Type.Optional(Type.Number({ minimum: 1 })),
    }),
    async execute(_toolCallId, params, signal, onUpdate, ctx) {
      await client.ensureAvailable(signal);
      const snapshot = await client.rpc<{ latestSequence: number }>("hub.snapshot", {}, signal);
      const spawned = await client.rpc<{ agentId: string; runId: string }>(
        "agent.spawn",
        {
          profile: params.agent,
          prompt: params.prompt,
          background: params.background ?? false,
          model: params.model,
          access: params.access,
          isolated: params.isolated ?? false,
          maxRuntimeSeconds: params.maxRuntimeSeconds,
          cwd: ctx.cwd,
          parentAgentId: process.env.AGENT_HUB_PARENT_AGENT_ID,
          rootSessionId: ctx.sessionManager.getSessionId(),
          idempotencyKey: _toolCallId,
        },
        signal,
      );
      if (params.background) {
        return {
          content: [
            {
              type: "text" as const,
              text: `Started background agent ${spawned.agentId} with run ${spawned.runId}.`,
            },
          ],
          details: { ...spawned, background: true },
        };
      }
      const abort = () => {
        void client.rpc("agent.abort", {
          agentId: spawned.agentId,
          idempotencyKey: `${_toolCallId}:abort`,
        });
      };
      signal?.addEventListener("abort", abort, { once: true });
      let output = "";
      try {
        const run = await client.waitForRun(
          spawned.runId,
          snapshot.latestSequence,
          (event) => {
            output = updateProgress(event, output);
            onUpdate?.({
              content: [{ type: "text", text: output.slice(-4000) }],
              details: { ...spawned, event: event.type },
            });
          },
          signal,
        );
        if (run.state !== "succeeded") {
          throw new Error(run.error ?? `Delegated run ${run.state}`);
        }
        return result(run, spawned);
      } finally {
        signal?.removeEventListener("abort", abort);
      }
    },
  });
}

function updateProgress(event: HubEvent, previous: string): string {
  if (event.type === "run.output.delta" && typeof event.data.text === "string") {
    return previous + event.data.text;
  }
  if (event.type === "run.tool.started") {
    return `${previous}\n[${String(event.data.toolName ?? "tool")}]`;
  }
  if (event.type === "run.state.changed") {
    return `${previous}\nRun ${String(event.data.state)}.`;
  }
  return previous;
}

function result(run: RunRecord, ids: { agentId: string; runId: string }) {
  return {
    content: [{ type: "text" as const, text: run.result ?? "" }],
    details: { ...ids, state: run.state },
    usage: normalizeUsage(run.usage),
  };
}

function normalizeUsage(raw: Record<string, unknown>): Usage {
  const tokens = record(raw.tokens);
  const cost = record(raw.cost);
  const input = number(tokens.input ?? raw.input ?? raw.inputTokens);
  const output = number(tokens.output ?? raw.output ?? raw.outputTokens);
  const cacheRead = number(tokens.cacheRead ?? raw.cacheRead ?? raw.cacheReadTokens);
  const cacheWrite = number(tokens.cacheWrite ?? raw.cacheWrite ?? raw.cacheWriteTokens);
  const totalCost = typeof raw.cost === "number" ? number(raw.cost) : number(cost.total);
  return {
    input,
    output,
    cacheRead,
    cacheWrite,
    totalTokens: number(tokens.total ?? raw.totalTokens) || input + output + cacheRead + cacheWrite,
    cost: {
      input: number(cost.input),
      output: number(cost.output),
      cacheRead: number(cost.cacheRead),
      cacheWrite: number(cost.cacheWrite),
      total: totalCost,
    },
  };
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : {};
}

function number(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}
