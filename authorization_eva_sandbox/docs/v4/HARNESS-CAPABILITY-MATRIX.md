# V4 harness capability matrix

The common integration is a trusted evaluator plus an allowlisted tool gateway
and a harness-owned agent workspace. Container topology is adapter-specific.

## Builtin ReAct

Retired on the clean migration branch. It is not an allowed harness for
tests, calibration, experiments, conformance, or benchmark results. Historical
Builtin trajectories remain available only from the handoff/archive branch.

## OpenHands Software Agent SDK

- Tool transport: typed custom tools or MCP
- Workspace: local, Docker, or remote SDK workspace
- Events: typed conversation events / WebSocket through Agent Server
- Compaction: `LLMSummarizingCondenser` only when the task sets
  `agent.condenser.max_tokens`; otherwise the agent has no condenser.
  `max_size` stays at the SDK default of 240 events. The retired 28-event
  default is not used. `max_chars` is not a budget.
- V4 rule: `openhands-sdk==1.49.2` in `.venv-openhands`, with matching
  `openhands-tools`, `openhands-workspace`, and `openhands-agent-server`.
  Agents are built by `build_openhands_agent`, which passes the sealed
  prompt as `system_prompt`. `openhands-sdk` 1.15.0 and
  `openhands-ai==1.6.0` are not supported.
- V4 role: the first production harness and conformance target. Use the native
  Conversation/Agent loop; do not implement a replacement ReAct loop.
- Selected on 2026-09-21 as the harness to migrate to now. The agent learns
  tools from MCP `tools/list` on the task allowlist. OpenClaw and the OpenAI
  Agents SDK are later adapters, not this migration.

## OpenClaw

- Tool transport: trusted Gateway plugin or MCP
- Workspace: sandbox backend selected by OpenClaw
- Events: session transcript plus structured Gateway logs
- Compaction: persistent session compaction/provider
- V4 rule: normal and sandbox tool allowlists must both pass; elevated execution
  is disabled

## OpenAI Agents Python SDK

- Status: connected 2026-09-24, all eight settings validated; see the status
  section of [`HARNESS-OPENAI-AGENTS-PLAN.md`](HARNESS-OPENAI-AGENTS-PLAN.md)
- Tool transport: host function tools or MCP. The mounted tools are
  `terminal`, `read_file`, `write_file`, and `finish`, with the OpenHands
  names, parameters and descriptions. OpenHands' `security_risk` and
  `summary` fields are OpenHands-only and are not sent. Its `think` built-in
  is not mounted. Business tools use the gateway's own schemas.
- Workspace: the shared agent container. Since 2026-09-24 this plain `Agent`
  harness is the bare-loop control; the benchmark harness is `SandboxAgent`
  (below).
- Events: SDK items mapped onto the normalized trajectory, including
  reasoning and assistant text, tool calls and tool results
- Compaction: the task's `agent.condenser` budget, because condensation at
  that budget is part of the task. The SDK has no native condenser that can
  apply that budget on both the Responses path and the LiteLLM chat path.
- Prompt caching: Anthropic Sonnet profiles on the LiteLLM path, at the
  breakpoints OpenHands marks. GPT and Gemini cache without extra markers.
  GLM, locked to `z-ai`, gets no markers.
- V4 rule: function tools retain gateway credentials in the trusted runner

## OpenAI Agents SDK SandboxAgent (benchmark harness, decided 2026-09-24)

- Prompt: the SDK sandbox base prompt, then our text as `instructions`,
  then capability text and the rendered filesystem tree
- Tools: `Shell` (`exec_command`, `write_stdin`) and `Filesystem`
  (`apply_patch`, `view_image`), plus our task and business tools
- `apply_patch`: native custom tool on GPT (Responses); a function tool with
  the same name and description on the six Chat Completions settings,
  recorded per run
- Workspace: a sandbox session bound to the task's agent container
  (feasibility spike on `harness/sandbox2a`)
- Compaction: deferred. The SDK capability sends Responses
  `context_management`, which LiteLLM rejects on the OpenRouter chat
  path. This harness does not enable it and does not run our condenser.
  `Memory` stays off
- Status: connected 2026-09-24 (`openai_sandbox` on `main`). Offline wire
  check passed on all 8 settings. The 8-task native pilot (64 runs,
  including T0028) finished exit 0. Compaction stays off

## Claude Agent SDK

- Chosen on 2026-09-24 as the third harness, in place of the Tool Runner
  entry below. See Step 2 in
  [`HARNESS-OPENAI-AGENTS-PLAN.md`](HARNESS-OPENAI-AGENTS-PLAN.md).
- Prompt: `claude_code` preset with the task text in `append`
- Tool transport: in-process SDK MCP server for the task and business tools;
  Claude Code's built-ins stay except WebFetch, WebSearch and Agent
- Workspace: the CLI runs inside the agent container
  (`authorizationbench-claude-code:2.1.281`)
- Events: SDK message stream plus PreToolUse/PostToolUse hooks
- Compaction: Claude Code's native compaction. The aligned budget is the
  task's `max_tokens` plus this harness's native prompt tokens;
  `CLAUDE_CODE_MAX_CONTEXT_TOKENS` is that budget plus 33000
- Status: adapter and the 64-run pilot are on `harness/claude2a`
  (`71728cd1`); not merged to `main`

## Google ADK (noted 2026-09-24, not started)

- Prompt: `LlmAgent.instruction` is our text. No planner, no
  `GlobalInstructionPlugin`, no built-in search or code execution
- Tools: Python function tools around the existing workspace and business
  tools. `MemoryService` stays off
- Models: all 8 settings through `LiteLlm` (`litellm>=1.84`)
- Events: runner events, plus `before_model_callback`,
  `after_tool_callback`, and `on_tool_error_callback`
- Compaction: `EventsCompactionConfig.token_threshold` at the aligned
  budget. `event_retention_size` versus `keep_first` is still a probe
- Status: model probe in progress on `harness/adk`. See the ADK section in
  [`HARNESS-OPENAI-AGENTS-PLAN.md`](HARNESS-OPENAI-AGENTS-PLAN.md)

## Anthropic Python SDK Tool Runner (superseded by the Claude Agent SDK)

- Tool transport: application-executed custom tools
- Workspace: supplied by the V4 `AgentWorkspace` implementation
- Events: streamed messages/tool uses captured by the adapter
- Compaction: server-side context management; record compaction events when
  exposed by the API
- V4 rule: use the Python client Tool Runner, not Claude Code or Claude Agent
  SDK

## Required conformance

Every adapter must prove:

1. exact instruction and public context injection;
2. the same allowlisted business Tool IR;
3. denial of private config and environment controls;
4. complete business action, Oracle, shell, and file evidence;
5. deterministic fresh state and reset;
6. normalized trajectory and usage metadata;
7. adapter-specific context-pressure mapping;
8. an observable compaction event for trigger-g calibration;
9. no grader or provider credential in the agent workspace.

Official references:

- https://docs.openhands.dev/sdk/arch/overview
- https://docs.openhands.dev/sdk/arch/tool-system
- https://docs.openclaw.ai/gateway/sandboxing
- https://docs.openclaw.ai/gateway/sandbox-vs-tool-policy-vs-elevated
- https://docs.openclaw.ai/reference/session-management-compaction
- https://openai.github.io/openai-agents-python/sandbox/guide/
- https://openai.github.io/openai-agents-python/mcp/
- https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-runner
