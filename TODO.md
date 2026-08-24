# TODO

- [ ] Add a transactional `agent-hub update` command. Stage and validate the new package, back up the database, update the bundled extension, restart the service, verify `/health`, and roll back if any step fails.

## Agent catalog

- [ ] Add a curated agent registry under `registry/` in the Agent Hub repository. Store immutable, versioned bundles under `registry/agents/<owner>/<name>/<version>/` with `agent.toml`, `instructions.md`, `README.md`, and optional evaluations.
- [ ] Generate `registry/index.json` from the bundles in CI. Include searchable metadata, available versions, bundle paths, runtime and access requirements, and SHA-256 digests. Keep bundle files as the source of truth.
- [ ] Require maintainer review and passing schema, security, and evaluation checks before merging an agent. Reject modifications to an existing version and require changes to use a new version.
- [ ] Add the Agent Hub repository as the built-in catalog source so `agent-hub catalog` works without configuration.
- [ ] Add `agent-hub catalog`, `catalog search`, `catalog show`, and `catalog install` commands. Installing an agent must verify its digest, display its permissions and external dependencies, and never import credentials or session history.
- [ ] Add `agent-hub catalog source add`, `source list`, and `source remove` commands for additional compatible registries. Identify agents by `<owner>/<name>` so sources cannot silently shadow one another.
- [ ] Add `agent-hub agent list`, `agent update`, and `agent remove` commands for locally installed agents. Pin installed versions and require confirmation when an update expands tools, write access, network access, or MCP dependencies.
- [ ] Extend profile loading to support installed bundles while preserving local user overrides and project-profile opt-in.
- [ ] Document how to create, validate, and publish a registry, and how users configure sources, browse the catalog, install agents, update them, and inspect their permissions.

## CodePuppy runtime

- [x] Confirm that CodePuppy exposes a stable machine-readable API for prompting, streaming events, cancellation, and session restoration. Do not parse output intended for humans.
- [x] Add a `CodePuppyRuntime` adapter that implements `AgentRuntime` and keeps CodePuppy-specific lifecycle behavior outside the manager.
- [x] Normalize CodePuppy output, tool activity, usage, errors, and lifecycle changes into Agent Hub events. Report unsupported steering, follow-up, or restoration capabilities explicitly.
- [x] Support `runtime = "codepuppy"` in profiles, including executable discovery, model configuration, access controls, runtime limits, and workspace isolation.
- [x] Add protocol fixture tests and optional end-to-end tests covering completion, streaming, cancellation, crashes, malformed records, and restoration.
- [x] Document installation requirements, profile configuration, supported capabilities, and known limitations.

## Distributed workers

- [ ] Split the service into a central control plane and worker supervisors. Keep scheduling, desired state, and event history in the control plane. Keep runtime handles, subprocess I/O, signals, and workspaces on workers.
- [ ] Define an authenticated worker protocol for registration, capabilities, heartbeats, command leases, events, cancellation, and fencing tokens.
- [ ] Replace SQLite and in-memory coordination with PostgreSQL, durable command delivery, and distributed concurrency leases.
- [ ] Replace local `cwd` and session paths with workspace and artifact references backed by persistent volumes or object storage.
- [ ] Add a Kubernetes execution backend that runs one supervised agent runtime per pod and records its worker assignment and lease generation.
- [ ] Persist enough session and workspace state to resume agents after pod or node loss. Define retry behavior for runs that cannot resume safely.
- [ ] Use workload identity or mutual TLS for worker authentication. Prefer outbound worker connections so worker nodes do not require inbound ports.
- [ ] Add integration tests for worker loss, stale workers, duplicate commands, control-plane restarts, event replay, reassignment, and cancellation.
