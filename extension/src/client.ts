import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { request } from "node:http";

import { JSONLDecoder } from "./jsonl.js";

export type AgentState =
  | "starting"
  | "idle"
  | "running"
  | "parked"
  | "stopping"
  | "stopped"
  | "failed";

export type RunState = "queued" | "running" | "succeeded" | "failed" | "aborted";

export type AgentRecord = {
  id: string;
  runtime: string;
  profile: string;
  parentAgentId?: string;
  rootSessionId?: string;
  cwd: string;
  isolated: boolean;
  detached: boolean;
  currentTool?: string;
  state: AgentState;
  createdAt: string;
  updatedAt: string;
};

export type RunRecord = {
  id: string;
  agentId: string;
  state: RunState;
  prompt: string;
  createdAt?: string;
  startedAt?: string;
  settledAt?: string;
  result?: string;
  usage: Record<string, unknown>;
  error?: string;
};

export type HubEvent = {
  sequence: number;
  timestamp: string;
  type: string;
  agentId?: string;
  runId?: string;
  data: Record<string, unknown>;
};

type RPCResponse<T> = {
  jsonrpc: "2.0";
  id: string;
  result?: T;
  error?: { code: number; message: string; data?: unknown };
};

type EventNotification = {
  jsonrpc: "2.0";
  method: "agent.event";
  params: HubEvent;
};

const TERMINAL_STATES = new Set<RunState>(["succeeded", "failed", "aborted"]);

export class HubClient {
  constructor(readonly socketPath: string) {}

  async ensureAvailable(signal?: AbortSignal): Promise<void> {
    try {
      await this.health(signal);
      return;
    } catch (error) {
      if (signal?.aborted) {
        throw error;
      }
    }
    const executable = process.env.AGENT_HUB_COMMAND ?? "agent-hub";
    const child = spawn(executable, [], { detached: true, stdio: "ignore" });
    child.unref();
    for (let attempt = 0; attempt < 50; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 100));
      try {
        await this.health(signal);
        return;
      } catch (error) {
        if (signal?.aborted) {
          throw error;
        }
      }
    }
    throw new Error(`Agent Hub is unavailable at ${this.socketPath}`);
  }

  async health(signal?: AbortSignal): Promise<void> {
    await this.httpRequest("GET", "/health", undefined, signal);
  }

  async rpc<T>(
    method: string,
    params: Record<string, unknown> = {},
    signal?: AbortSignal,
  ): Promise<T> {
    const id = randomUUID();
    const body = Buffer.from(`${JSON.stringify({ jsonrpc: "2.0", id, method, params })}\n`);
    const response = await this.httpRequest("POST", "/v1/rpc", body, signal);
    const decoder = new JSONLDecoder<RPCResponse<T>>();
    const records = [...decoder.push(response), ...decoder.finish()];
    const record = records.find((item) => item.id === id);
    if (!record) {
      throw new Error("Agent Hub returned no matching JSON-RPC response");
    }
    if (record.error) {
      throw new Error(`${record.error.message} (${record.error.code})`);
    }
    if (record.result === undefined) {
      throw new Error("Agent Hub returned an empty result");
    }
    return record.result;
  }

  async waitForRun(
    runId: string,
    after: number,
    onEvent: (event: HubEvent) => void,
    signal?: AbortSignal,
  ): Promise<RunRecord> {
    let sequence = after;
    while (true) {
      const current = await this.rpc<{ run: RunRecord }>("run.get", { runId }, signal);
      if (TERMINAL_STATES.has(current.run.state)) {
        return current.run;
      }
      let consumed: { sequence: number; gap: boolean };
      try {
        consumed = await this.consumeEvents(runId, sequence, onEvent, signal);
      } catch (error) {
        if (signal?.aborted) {
          throw error;
        }
        await new Promise((resolve) => setTimeout(resolve, 100));
        await this.ensureAvailable(signal);
        continue;
      }
      sequence = consumed.sequence;
      if (consumed.gap) {
        const snapshot = await this.rpc<{ latestSequence: number }>("hub.snapshot", {}, signal);
        sequence = snapshot.latestSequence;
      }
    }
  }

  private consumeEvents(
    runId: string,
    after: number,
    onEvent: (event: HubEvent) => void,
    signal?: AbortSignal,
  ): Promise<{ sequence: number; gap: boolean }> {
    return new Promise((resolve, reject) => {
      let sequence = after;
      let terminal = false;
      let gap = false;
      const decoder = new JSONLDecoder<EventNotification>();
      const req = request(
        { socketPath: this.socketPath, path: `/v1/events?after=${after}`, method: "GET" },
        (response) => {
          if (response.statusCode !== 200) {
            response.resume();
            reject(new Error(`Agent Hub event stream returned HTTP ${response.statusCode}`));
            return;
          }
          response.on("data", (chunk: Buffer) => {
            try {
              for (const notification of decoder.push(chunk)) {
                const event = notification.params;
                if (event.sequence > sequence + 1) {
                  gap = true;
                  response.destroy();
                  return;
                }
                sequence = event.sequence;
                onEvent(event);
                if (
                  event.runId === runId &&
                  event.type === "run.state.changed" &&
                  TERMINAL_STATES.has(event.data.state as RunState)
                ) {
                  terminal = true;
                  response.destroy();
                }
              }
            } catch (error) {
              response.destroy();
              reject(error);
            }
          });
          response.on("close", () => resolve({ sequence, gap }));
          response.on("error", (error) => {
            if (terminal) {
              resolve({ sequence, gap });
            } else {
              reject(error);
            }
          });
        },
      );
      const abort = () => req.destroy(new Error("Agent Hub request aborted"));
      signal?.addEventListener("abort", abort, { once: true });
      req.on("error", reject);
      req.on("close", () => signal?.removeEventListener("abort", abort));
      req.end();
    });
  }

  private httpRequest(
    method: "GET" | "POST",
    path: string,
    body?: Buffer,
    signal?: AbortSignal,
  ): Promise<Buffer> {
    return new Promise((resolve, reject) => {
      const req = request(
        {
          socketPath: this.socketPath,
          path,
          method,
          headers: body
            ? { "content-type": "application/x-ndjson", "content-length": String(body.length) }
            : undefined,
        },
        (response) => {
          const chunks: Buffer[] = [];
          response.on("data", (chunk: Buffer) => chunks.push(chunk));
          response.on("end", () => {
            if (response.statusCode === undefined || response.statusCode >= 400) {
              reject(new Error(`Agent Hub returned HTTP ${response.statusCode ?? "unknown"}`));
              return;
            }
            resolve(Buffer.concat(chunks));
          });
          response.on("error", reject);
        },
      );
      const abort = () => req.destroy(new Error("Agent Hub request aborted"));
      signal?.addEventListener("abort", abort, { once: true });
      req.on("error", reject);
      req.on("close", () => signal?.removeEventListener("abort", abort));
      if (body) {
        req.write(body);
      }
      req.end();
    });
  }
}
