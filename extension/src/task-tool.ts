import { getMarkdownTheme, keyHint, type ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Container, Markdown, Spacer, Text } from "@earendil-works/pi-tui";
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

type ToolActivity = {
  name: string;
  count: number;
};

type TaskDetails = {
  agentId: string;
  runId: string;
  profile: string;
  state: string;
  startedAt: number;
  updatedAt: number;
  toolCalls: ToolActivity[];
  currentTool?: string;
  latestText?: string;
  background?: boolean;
};

type TaskProgress = {
  output: string;
  details: TaskDetails;
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
      model: Type.Optional(
        Type.Union([Type.String(), Type.Null()], {
          description: "Model override, or null to use the profile default",
        }),
      ),
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
      const progress: TaskProgress = {
        output: "",
        details: {
          ...spawned,
          profile: params.agent,
          state: params.background ? "background" : "starting",
          startedAt: Date.now(),
          updatedAt: Date.now(),
          toolCalls: [],
          background: params.background ?? false,
        },
      };
      if (params.background) {
        return {
          content: [
            {
              type: "text" as const,
              text: `Started background agent ${spawned.agentId} with run ${spawned.runId}.`,
            },
          ],
          details: taskDetails(progress.details),
        };
      }
      onUpdate?.(progressResult(progress));
      const heartbeat = setInterval(() => {
        progress.details.updatedAt = Date.now();
        onUpdate?.(progressResult(progress));
      }, 1000);
      heartbeat.unref();
      const abort = () => {
        void client.rpc("agent.abort", {
          agentId: spawned.agentId,
          idempotencyKey: `${_toolCallId}:abort`,
        });
      };
      signal?.addEventListener("abort", abort, { once: true });
      try {
        const run = await client.waitForRun(
          spawned.runId,
          snapshot.latestSequence,
          (event) => {
            updateProgress(event, progress);
            onUpdate?.(progressResult(progress));
          },
          signal,
        );
        if (run.state !== "succeeded") {
          throw new Error(run.error ?? `Delegated run ${run.state}`);
        }
        progress.details.state = run.state;
        progress.details.currentTool = undefined;
        progress.details.updatedAt = Date.now();
        return result(run, progress.details);
      } finally {
        clearInterval(heartbeat);
        signal?.removeEventListener("abort", abort);
      }
    },
    renderCall(args, theme, _context) {
      let text = theme.fg("toolTitle", theme.bold("task "));
      text += theme.fg("accent", args.agent);
      const modes = [
        args.background ? "background" : undefined,
        args.isolated ? "isolated" : undefined,
        args.access,
      ].filter((value): value is string => typeof value === "string" && value.length > 0);
      if (modes.length > 0) {
        text += theme.fg("muted", ` [${modes.join(", ")}]`);
      }
      text += `\n  ${theme.fg("dim", preview(args.prompt, 120))}`;
      return new Text(text, 0, 0);
    },
    renderResult(toolResult, { expanded, isPartial }, theme, context) {
      const details = taskResultDetails(toolResult.details);
      const content = toolResult.content[0];
      const output = content?.type === "text" ? content.text.trim() : "";
      if (!details) {
        return new Text(
          theme.fg(context.isError ? "error" : "toolOutput", output || "No output"),
          0,
          0,
        );
      }

      const duration = formatDuration(details.updatedAt - details.startedAt);
      const toolCount = details.toolCalls.reduce((total, item) => total + item.count, 0);
      const activity = `${duration} · ${toolCount} ${toolCount === 1 ? "tool call" : "tool calls"}`;

      if (isPartial) {
        const status =
          details.state === "starting"
            ? "Starting"
            : details.state === "queued"
              ? "Queued"
              : "Running";
        let text = `${theme.fg("warning", "●")} ${theme.fg("toolTitle", theme.bold(status))}`;
        text += theme.fg("dim", ` · ${activity}`);
        if (details.currentTool) {
          text += `\n  ${theme.fg("muted", "Current: ")}${theme.fg("accent", details.currentTool)}`;
        }
        if (details.toolCalls.length > 0) {
          text += `\n  ${theme.fg("muted", "Tools: ")}${theme.fg("dim", formatTools(details.toolCalls))}`;
        }
        if (details.latestText) {
          text += `\n  ${theme.fg("muted", "Latest: ")}${theme.fg("dim", details.latestText)}`;
        }
        return new Text(text, 0, 0);
      }

      if (details.background) {
        return new Text(
          `${theme.fg("accent", "→")} ${theme.fg("toolTitle", theme.bold("Started in background"))}`,
          0,
          0,
        );
      }

      const failed = context.isError || details.state !== "succeeded";
      const icon = theme.fg(failed ? "error" : "success", failed ? "✗" : "✓");
      const status = failed ? "Failed" : "Completed";
      const header = `${icon} ${theme.fg("toolTitle", theme.bold(status))}${theme.fg("dim", ` · ${activity}`)}`;

      if (expanded && output) {
        const container = new Container();
        container.addChild(new Text(header, 0, 0));
        if (details.toolCalls.length > 0) {
          container.addChild(
            new Text(theme.fg("dim", `Tools: ${formatTools(details.toolCalls)}`), 0, 0),
          );
        }
        container.addChild(new Spacer(1));
        container.addChild(new Markdown(output, 0, 0, getMarkdownTheme()));
        return container;
      }

      let text = header;
      if (output) {
        const lines = output.split("\n");
        const visible = lines.slice(0, 6);
        text += `\n${theme.fg("toolOutput", visible.join("\n"))}`;
        if (lines.length > visible.length) {
          text += `\n${theme.fg("muted", keyHint("app.tools.expand", "to expand"))}`;
        }
      }
      return new Text(text, 0, 0);
    },
  });
}

function updateProgress(event: HubEvent, progress: TaskProgress): void {
  progress.details.updatedAt = Date.now();
  if (event.type === "run.output.delta" && typeof event.data.text === "string") {
    progress.output = (progress.output + event.data.text).slice(-4000);
    const lines = progress.output
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
    const latest = lines.at(-1);
    progress.details.latestText = latest ? preview(latest, 160) : undefined;
  } else if (event.type === "run.tool.started") {
    const name = typeof event.data.toolName === "string" ? event.data.toolName : "tool";
    const activity = progress.details.toolCalls.find((item) => item.name === name);
    if (activity) {
      activity.count += 1;
    } else {
      progress.details.toolCalls.push({ name, count: 1 });
    }
    progress.details.currentTool = name;
  } else if (event.type === "run.tool.finished") {
    progress.details.currentTool = undefined;
  } else if (event.type === "run.state.changed" && typeof event.data.state === "string") {
    progress.details.state = event.data.state;
  } else if (event.type === "run.error" && typeof event.data.message === "string") {
    progress.details.latestText = preview(event.data.message, 160);
  }
}

function progressResult(progress: TaskProgress) {
  return {
    content: [{ type: "text" as const, text: progressText(progress.details) }],
    details: taskDetails(progress.details),
  };
}

function progressText(details: TaskDetails): string {
  let text = `${details.profile}: ${details.state}`;
  if (details.currentTool) {
    text += ` - ${details.currentTool}`;
  }
  if (details.latestText) {
    text += `\n${details.latestText}`;
  }
  return text;
}

function result(run: RunRecord, details: TaskDetails) {
  return {
    content: [{ type: "text" as const, text: run.result ?? "" }],
    details: taskDetails(details),
    usage: normalizeUsage(run.usage),
  };
}

function taskDetails(details: TaskDetails): TaskDetails {
  return {
    ...details,
    toolCalls: details.toolCalls.map((item) => ({ ...item })),
  };
}

function taskResultDetails(value: unknown): TaskDetails | undefined {
  if (typeof value !== "object" || value === null) {
    return undefined;
  }
  const details = value as Partial<TaskDetails>;
  if (
    typeof details.agentId !== "string" ||
    typeof details.runId !== "string" ||
    typeof details.profile !== "string" ||
    typeof details.state !== "string" ||
    typeof details.startedAt !== "number" ||
    typeof details.updatedAt !== "number" ||
    !Array.isArray(details.toolCalls)
  ) {
    return undefined;
  }
  return details as TaskDetails;
}

function formatTools(tools: ToolActivity[]): string {
  return tools.map((item) => `${item.name}${item.count > 1 ? ` ×${item.count}` : ""}`).join(" · ");
}

function formatDuration(milliseconds: number): string {
  const seconds = Math.max(0, Math.round(milliseconds / 1000));
  if (seconds < 60) {
    return `${seconds}s`;
  }
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

function preview(value: string, limit: number): string {
  const compact = value.replace(/\s+/g, " ").trim();
  return compact.length > limit ? `${compact.slice(0, limit - 3)}...` : compact;
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
