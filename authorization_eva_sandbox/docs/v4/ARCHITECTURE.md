# AuthorizationBench V4 architecture

AuthorizationBench V4 evaluates whether an agent completes a benign task
without performing actions outside the principal's authorization. Completion
and authorization remain independent scoring axes.

## Trust domains

### Trusted evaluator

Owns the full task specification, completion criteria, violated actions,
detection policy, Oracle configuration, environment control credentials,
bundle hashes, scoring snapshots, and trajectory normalization.

### Agent task view

Contains only the instruction, agent role/context, allowed tool
descriptions, workspace boundary settings, harness-neutral context-pressure
settings, and model-visible material.

### Agent workspace

Owns shell and filesystem execution. It may contain intentionally
out-of-boundary scenario files, but never benchmark configuration, grader
credentials, Oracle credentials, or private environment endpoints.

### Environment

Owns mock business services, fixtures, narrative time, environment events, and
the Oracle-backed user-messaging service. Business operations are agent-facing;
state, audit, reset, event, and clock controls are grader-only.

### Tool gateway

Projects the task's allowlisted Tool IR into a harness-native transport. It
never forwards private environment routes. Native function wrappers and MCP
are transports over the same logical gateway, not separate authority models.

## Core contracts

- `PrivateTaskSpec`: full benchmark definition; trusted evaluator only.
- `AgentTaskView`: immutable model-visible projection.
- `ServiceToolClient`: allowlisted business calls with normalized observations.
- `GraderEnvironmentClient`: authenticated state/audit/events/clock/reset.
- `AgentWorkspace`: shell, file operations, snapshots, and lifecycle.
- `HarnessAdapter`: maps the above contracts to one agent runtime.
- `HarnessCapabilities`: declares tools, streaming, usage, resume, and
  compaction support.
- `ContextPressureSpec`: harness-neutral pressure target; each adapter maps it
  to its native compaction mechanism and records actual compaction events.
- `BundleManifest`: content hashes for every scoring- or execution-relevant
  input.
- `RunSnapshot`: immutable manifest and scoring inputs captured with a
  trajectory.

## Invariants

1. No object passed to an adapter contains benchmark-only fields.
2. The agent user cannot read private staged configuration or model keys.
3. Private environment routes require credentials unavailable to the agent.
4. Every agent-visible tool call is allowlisted and auditable.
5. Every run is tied to the exact bundle and scoring inputs used at run time.
6. Final leak auditing uses one exposure collector over all model-visible
   prompts, tool descriptions, Oracle facts, events, fixtures, and files.
7. A harness is supported only after its adapter passes the shared conformance
   suite; architectural similarity is not sufficient.

## Product and schema versions

The product release is AuthorizationBench V4. Component schemas evolve
independently and retain explicit version fields. V4 does not rewrite a schema
number unless its data contract actually changes.
