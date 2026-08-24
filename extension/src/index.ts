import { homedir } from "node:os";
import { join } from "node:path";

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

import { HubClient } from "./client.js";
import { showHub } from "./hub.js";
import { registerTaskTool } from "./task-tool.js";

export default function registerAgentHub(pi: ExtensionAPI): void {
  const socketPath =
    process.env.AGENT_HUB_SOCKET ?? join(homedir(), ".agent-hub", "run", "agent-hub.sock");
  const client = new HubClient(socketPath);

  registerTaskTool(pi, client);
  pi.registerCommand("hub", {
    description: "View and control Agent Hub agents",
    handler: async (_arguments, ctx) => {
      try {
        await showHub(ctx, client);
      } catch (error) {
        ctx.ui.notify(error instanceof Error ? error.message : String(error), "error");
      }
    },
  });
}
