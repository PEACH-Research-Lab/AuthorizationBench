# Playbook: connecting a new agent harness

The order and checks below come from connecting OpenHands 1.49.2 and the
OpenAI Agents SDK. Each step names the problem it prevents. The plan for
the individual harnesses is in `HARNESS-OPENAI-AGENTS-PLAN.md`; what each
harness can do is in `HARNESS-CAPABILITY-MATRIX.md`. Machine facts
(images, venvs, provider locks) and how to launch, rejudge and view runs
are in `BENCHMARK-RUNBOOK.md`.

## What is the same on every harness, and what is not

Decided 2026-09-24: what defines the task is the same on every harness;
what defines the harness follows the harness. Different harnesses are
expected to give different results. Nothing is tuned so that one harness
matches another.

The same on every harness (the task):

- the sealed system prompt, appended after the harness's own prompt
  (decided 2026-09-24, see "System prompt composition" in
  `HARNESS-OPENAI-AGENTS-PLAN.md`), byte for byte, except the shared
  `tool_name_adapter` that shows every model `service_operation` instead
  of `service.operation` (model APIs reject dots) on every harness;
- every tool the task's prompt names, with the same name, parameters and
  description: the business tools (through the MCP gateway allowlist),
  `terminal`, `read_file`, `write_file` and `finish`. The descriptions of
  the last four are the texts OpenHands 1.49.2 ships; they are treated as
  the task's tool documentation. No other renaming: a prefix a harness
  adds itself (Claude Code's `mcp__task__`) is left in place and the
  prompt is not rewritten for it;
- the task's condensation: when the task sets `agent.condenser`, the
  context is condensed at that token budget on every harness, with the
  harness's native mechanism where it can honour the budget, otherwise
  with ours;
- the agent container (image `authorizationbench-openhands:1.49.2`,
  mounts, user, network);
- the model request per setting: model, provider lock, effort, output cap;
- the simulated user (`oracle_reply_fn`, MiMo-V2.6-Pro);
- the budgets: steps per segment, simulated-user turns, wall clock;
- the evidence the judges read: service audit, file contents, attachment
  bytes, the agent's messages and reasoning.

Not copied from another harness: anything the task does not ask for and
one harness adds on its own, such as OpenHands' `security_risk` and
`summary` tool fields and its `think` tool (OpenHands keeps all three;
no other harness gets them). A harness's own tools are kept when they can
run inside the sandbox and be recorded; otherwise they are disabled and
the limitation recorded. Claude Code (decided 2026-09-24): the CLI runs
inside the agent container (a separate image layered on the shared one),
its default built-in tools are kept except WebFetch and WebSearch, which
leave the sandbox; `Agent` subagents stay if their actions are recorded
in full, otherwise they are disabled; `--bare` is not used, so the
environment text Claude Code appends to the prompt is recorded per run.
Prompt caching is a deployment setting and is enabled
wherever the provider supports it.

Every harness-native behavior is recorded per run.

## Order of work

1. **Record requests before writing the adapter.** Put a recording
   endpoint between the harness and the model and save every request.
   Check it against the task-defining list above: system prompt, business
   tools, effort field, provider lock, output cap. Record everything else
   the harness adds (identity text, extra fields, tools) as harness-native.
   *Prevents:* surprises in what the model actually receives, such as a
   dropped effort field or an injected prompt.

2. **Pin dependencies in a separate environment.** One venv per harness
   (`.venv-<harness>`), installing `requirements-core.txt` plus the SDK.
   Resolve conflicts first; a proxy or helper with other pins gets its own
   venv. *Prevents:* openai-agents 0.22.3 needs openai>=3 while litellm
   1.102.0 needs openai<3.

3. **Call every setting for real, with several tool calls in a row.** All 8
   settings, at least three consecutive tool calls each, checking that
   later calls succeed and carry reasoning tokens, and that effort reaches
   the provider in the form it expects. Record the path used per run.
   *Prevents:* Gemini thought signatures replay only through
   `LitellmModel`; `parallel_tool_calls` returns 404 on the locked
   Anthropic endpoint.

4. **Isolate the harness from the operator's machine.** No user or project
   settings, a temporary HOME, telemetry off, a neutral working directory,
   every built-in tool that would run on the host disabled. Plant a marker
   in the operator's config and check it never reaches a request.

5. **Write down the end-of-run rules and get them approved before coding.**
   Budgets are the same on every harness; how a segment ends follows the
   harness. For each rule, state what OpenHands does and what the new
   harness will do:
   - step limit: per segment (OpenHands resets it on every `run()`);
   - repeated identical actions: recorded in `metadata.repeated_actions`,
     not a reason to stop;
   - simulated-user turns: up to 10 plain-reply turns;
   - wall clock: checked before each model call, covers the whole run;
   - output cap: handled and recorded as a termination;
   - the `finish` tool, which every task prompt lists, ends a segment.
   *Prevents:* the OpenAI adapter first stopped on repeated actions and
   counted steps across segments, both against decisions already made.

6. **Record everything the judges need.** Business actions from the
   service audit; file writes and edits with their content; attachments
   with their bytes (and PDF text); the agent's messages and reasoning; any
   harness-native summary or compaction, with when it fired and what it
   said; the harness's own session files. *Prevents:* a PDF attachment was
   stored as truncated text and file_edit content was lost, so the judge
   could not see what the agent sent.

7. **Context management.** A task with `agent.condenser` is condensed at
   its token budget on every harness: with the harness's native mechanism
   where it can be set to that budget (Claude Code: declared window =
   budget + 33000), otherwise with our condenser (OpenAI Agents SDK).
   Record each condensation: when it fired, what was dropped, the summary.

8. **Output layout and tooling.** Outputs under
   `outputs/<harness>/<setting>/`, reports under
   `post_hoc_eval_report/<harness>/<setting>/`, run numbers claimed with an
   atomic `mkdir`, the harness name added to `run_eval --harness`,
   `run_records` (`HARNESS_TITLES`) and `bench_plan`.

9. **Validate in stages.**
   1. A scripted run with no model (saved oracle scripts from
      `.oracle_runs/`): every tool call and result appears in order, the
      service audit agrees, the violating script leaves the violating state.
   2. One task per setting.
   3. The 6-task set (T0003-g-b-h, T0027-g-b-h, T0015-j-c-h,
      T0028-j-b-h, T0030-h-b-n, T0049-a-j-h) on all 8 settings. The check
      is that every run is valid and fully recorded, not that verdicts
      match another harness.
   Runs made before a fix that changes behavior are marked superseded.

## Rules that apply throughout

- Shared services and shared evaluation code change only with the user's
  approval.
- After changing anything the agent sees, run
  `quality_review.fix_check verify`.
- API keys never enter the container, the workspace, the trajectory, logs
  or commits.
- Before merging a change to shared code, one OpenHands run of a known task
  must match its previous result.
