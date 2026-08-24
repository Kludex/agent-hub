import { randomUUID } from "node:crypto";

import type { ExtensionCommandContext } from "@earendil-works/pi-coding-agent";

import type { AgentRecord, HubEvent, RunRecord } from "./client.js";
import { HubClient } from "./client.js";

type Snapshot = {
  agents: AgentRecord[];
  activeRuns: RunRecord[];
  latestSequence: number;
};

type AgentDetail = {
  agent: AgentRecord;
  runs: RunRecord[];
  events: HubEvent[];
};

export async function showHub(ctx: ExtensionCommandContext, client: HubClient): Promise<void> {
  await client.ensureAvailable();
  while (true) {
    let snapshot: Snapshot;
    try {
      snapshot = await client.rpc<Snapshot>("hub.snapshot");
    } catch {
      await client.ensureAvailable();
      snapshot = await client.rpc<Snapshot>("hub.snapshot");
    }
    if (snapshot.agents.length === 0) {
      ctx.ui.notify("Agent Hub has no agents.", "info");
      return;
    }
    const labels = snapshot.agents.map((agent) => label(agent, snapshot.activeRuns));
    const choice = await ctx.ui.select("Agent Hub", [...labels, "Close"]);
    if (!choice || choice === "Close") {
      return;
    }
    const index = labels.indexOf(choice);
    if (index < 0) {
      continue;
    }
    try {
      await inspectAgent(ctx, client, snapshot.agents[index]!);
    } catch {
      await client.ensureAvailable();
    }
  }
}

async function inspectAgent(
  ctx: ExtensionCommandContext,
  client: HubClient,
  selected: AgentRecord,
): Promise<void> {
  const detail = await client.rpc<AgentDetail>("agent.get", { agentId: selected.id });
  const actions = availableActions(detail.agent);
  const action = await ctx.ui.select(`${detail.agent.profile} - ${detail.agent.state}`, [
    "Transcript",
    ...actions,
    "Back",
  ]);
  if (!action || action === "Back") {
    return;
  }
  if (action === "Transcript") {
    await ctx.ui.editor(`${detail.agent.profile} transcript`, transcript(detail));
    return;
  }
  if (action === "Follow live output") {
    const run = [...detail.runs].reverse().find((item) => item.state === "running");
    if (!run) {
      return;
    }
    const snapshot = await client.rpc<Snapshot>("hub.snapshot");
    let status = `${detail.agent.profile}: running`;
    ctx.ui.setStatus("agent-hub-follow", status);
    try {
      await client.waitForRun(run.id, snapshot.latestSequence, (event) => {
        if (event.type === "run.output.delta" && typeof event.data.text === "string") {
          status = `${detail.agent.profile}: ${event.data.text.slice(-80)}`;
        } else if (event.type.startsWith("run.tool.")) {
          status = `${detail.agent.profile}: ${String(event.data.toolName ?? "tool")}`;
        } else if (event.type === "run.state.changed") {
          status = `${detail.agent.profile}: ${String(event.data.state)}`;
        }
        ctx.ui.setStatus("agent-hub-follow", status);
      });
    } finally {
      ctx.ui.setStatus("agent-hub-follow", undefined);
    }
    const updated = await client.rpc<AgentDetail>("agent.get", { agentId: detail.agent.id });
    await ctx.ui.editor(`${detail.agent.profile} transcript`, transcript(updated));
    return;
  }
  if (action === "Inspect patch") {
    const result = await client.rpc<{ patch: string; truncated: boolean }>("agent.patch", {
      agentId: detail.agent.id,
    });
    const suffix = result.truncated ? "\n\n[Patch truncated by Agent Hub]" : "";
    await ctx.ui.editor(`${detail.agent.profile} patch`, result.patch + suffix);
    return;
  }
  if (action === "Return result to editor") {
    const result = [...detail.runs].reverse().find((run) => run.result)?.result;
    if (result) {
      ctx.ui.setEditorText(result);
    }
    return;
  }
  if (action === "Steer" || action === "Follow up" || action === "Prompt") {
    const message = await ctx.ui.input(`${action} ${detail.agent.profile}`, "Enter a message");
    if (!message) {
      return;
    }
    const method =
      action === "Steer"
        ? "agent.steer"
        : action === "Follow up"
          ? "agent.follow_up"
          : "agent.prompt";
    const key = action === "Prompt" ? "prompt" : "message";
    await client.rpc(method, {
      agentId: detail.agent.id,
      [key]: message,
      idempotencyKey: randomUUID(),
    });
    return;
  }
  if (
    (action === "Apply patch" || action === "Discard worktree") &&
    !(await ctx.ui.confirm(action, `Confirm ${action.toLowerCase()} for ${detail.agent.profile}?`))
  ) {
    return;
  }
  const methods: Record<string, string> = {
    Abort: "agent.abort",
    Stop: "agent.stop",
    Park: "agent.park",
    Revive: "agent.revive",
    "Apply patch": "agent.apply",
    "Discard worktree": "agent.discard",
  };
  const method = methods[action];
  if (method) {
    await client.rpc(method, { agentId: detail.agent.id, idempotencyKey: randomUUID() });
  }
}

function availableActions(agent: AgentRecord): string[] {
  const isolation = agent.isolated ? ["Inspect patch", "Apply patch", "Discard worktree"] : [];
  if (agent.state === "running") {
    return ["Follow live output", "Steer", "Follow up", "Abort", "Stop"];
  }
  if (agent.state === "idle") {
    return ["Prompt", "Park", "Stop", "Return result to editor", ...isolation];
  }
  if (agent.state === "parked") {
    return ["Revive", "Stop", "Return result to editor", ...isolation];
  }
  return ["Return result to editor", ...isolation];
}

function label(agent: AgentRecord, activeRuns: RunRecord[]): string {
  const run = activeRuns.find((item) => item.agentId === agent.id);
  const task = run ? ` - ${run.prompt.slice(0, 50)}` : "";
  const parent = agent.parentAgentId ? ` parent:${agent.parentAgentId.slice(0, 8)}` : "";
  const usage = run ? usageLabel(run.usage) : "";
  const tool = agent.currentTool ? ` tool:${agent.currentTool}` : "";
  const elapsed = run?.startedAt ? ` ${elapsedLabel(run.startedAt)}` : "";
  return `${agent.profile} [${agent.runtime}] ${agent.state}${task}${tool}${usage}${elapsed}${parent} (${agent.id.slice(0, 12)})`;
}

function elapsedLabel(startedAt: string): string {
  const seconds = Math.max(0, Math.floor((Date.now() - Date.parse(startedAt)) / 1000));
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m${seconds % 60}s`;
}

function usageLabel(usage: Record<string, unknown>): string {
  const nestedTokens = object(usage.tokens);
  const nestedCost = object(usage.cost);
  const totalTokens = number(usage.totalTokens ?? nestedTokens.total);
  const totalCost = number(typeof usage.cost === "number" ? usage.cost : nestedCost.total);
  const tokens = totalTokens ? ` ${totalTokens} tokens` : "";
  const cost = totalCost ? ` $${totalCost.toFixed(4)}` : "";
  return tokens + cost;
}

function object(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : {};
}

function number(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function transcript(detail: AgentDetail): string {
  const lines = [`# ${detail.agent.profile}`, ""];
  for (const run of detail.runs) {
    lines.push(`## ${run.state}: ${run.prompt}`, "");
    const events = detail.events.filter((event) => event.runId === run.id);
    for (const event of events) {
      if (event.type === "run.output.delta" && typeof event.data.text === "string") {
        lines.push(event.data.text);
      } else if (event.type.startsWith("run.tool.")) {
        lines.push(`\n[${String(event.data.toolName ?? event.type)}]\n`);
      }
    }
    if (run.result) {
      lines.push("", run.result);
    }
    if (run.error) {
      lines.push("", `Error: ${run.error}`);
    }
    lines.push("");
  }
  return lines.join("\n");
}
