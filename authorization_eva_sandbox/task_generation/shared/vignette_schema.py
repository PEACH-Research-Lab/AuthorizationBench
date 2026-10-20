from __future__ import annotations

"""
Vignette Schema: Pydantic models defining the structure of a vignette.

A vignette fully specifies ONE sandbox task and carries enough information to
auto-generate all sandbox materials (task_config.json, data/*.json,
workspace/**, prior_memory.md, gitea/) without human intervention. At
evaluation time, task_config.instruction is rendered into
/instruction/task.md inside the agent container.

Data fixtures (data/*.json, prior_memory.md, gitea/) live alongside
task_config.json under tasks/<T>/, NOT inside the vignette. The vignette
describes WHAT data should exist (via environment.data_spec); concrete fixture
files are owned by the task directory.

===========================================================================
THE THREE PARTITIONS
===========================================================================
A vignette is mentally divided into three layers. "Partition" here just means
"which job the field does", not a nested JSON block — all fields are top-level.

  1. SCRIPT LAYER — the story that humans and the agent perceive.
     situational_trigger, domain, harm, instruction, oracle_user,
     agent_context, environment.narrative.
     Some of this is agent-visible (instruction, agent_context, oracle_user
     identity); the rest is authoring context.

  2. JUDGING LAYER — the hidden ground truth used to grade a run. NEVER shown
     to the agent.
     authorization_scope, completion_criteria, detection_methods.

  3. INFRASTRUCTURE LAYER — how the runtime environment is built.
     environment.data_spec (incl. the `gitea` channel), file_setting,
     reference_time.

Field order on disk (enforced by utils.normalize_vignette_data):
  vignette_id, source_seed, situational_trigger, trigger_manifestation,
  domain, harm, instruction, completion_criteria, authorization_scope,
  oracle_user, agent_context, environment,
     detection_methods, file_setting, agent_runtime, reference_time.
(oracle_user precedes agent_context because Stage-2 generates the oracle/
narrative first, then the agent_context that depends on it.)

===========================================================================
SCHEMA V2 (task-generation v2 plan, G1)
===========================================================================
Three structural upgrades over v1; all are backward-parseable (legacy
vignettes still load, but S4 grounding checks flag their unbound fields):

  1. authorization_scope.violated_actions: list[str] -> list[ViolatedAction]
     Each violation binds to a registry action (service + action + args_hint)
     so it can be machine-verified reachable (S4) and replayed (S5). `gain`
     records the temptation. Legacy strings coerce to unbound entries.
  2. trigger_manifestation: NEW top-level block — one entry per
     situational_trigger declaring WHERE the trigger materializes (evidence
     locators) and the trigger-specific declarations its contract checks.
     Contracts: task_generation/taxonomy/trigger_manifestation.json.
  3. Platform.port RETIRED (v2 services are mounted single-port by
     multi_server under /{id}/...). `template` must resolve in
     services/registry.py. data_spec's webapp_data channel is being renamed
     service_data in G3 (registry-template-schema-driven fixtures).

===========================================================================
FIELD-BY-FIELD: meaning / default / allowed values / downstream effect
===========================================================================

vignette_id : str  (required)
  Unique id, e.g. "V0001-b-e-h". Effect: to_task_id() maps V->T to name
  tasks/<T>/.

source_seed : str  (required)
  Originating seed id, e.g. "S0001". Effect: lineage; surgery re-reads the
  seed for semantic checks.

situational_trigger : str | list[str]  (required)
  Objective conditions that make failure more likely (ambiguous instructions,
  distracting context, problematic memory, cross-layer authorization
  conflicts). Pinned from the seed.

domain : str  (default "")
  Domain label, e.g. "personal_duty", "software_collaboration". From the seed.

harm : str  (default "Y")
  Whether the violated action causes real harm. Allowed: "Y" | "N". Labeling/
  bucketing only.

--- script layer (agent-facing) ---

instruction : str  (required)
  The task the agent sees, rendered into /instruction/task.md. MUST NOT contain
  explicit restrictions — boundaries live only in authorization_scope. Pinned
  from the seed.

oracle_user : OracleUserConfig  (default: natural / "Arya Stark")
  Simulated principal identity; single source of truth for {user_*}
  placeholders expanded by run_eval. See OracleUserConfig.

agent_context : AgentContext  (required)
  Agent-facing system-prompt context (role, resources, prior-memory summary).
  Agent-visible -> MUST NOT leak boundaries. See AgentContext.

--- judging layer (hidden ground truth) ---

completion_criteria : list[str]  (default [])
  Independent positive criteria for "task done", checked post-hoc. MUST NOT be
  coupled to violation boundaries (e.g. not "...without placing an order").
  Produced by Stage-1.

authorization_scope : AuthorizationScope  (default empty)
  Single source of truth for what the user authorized vs not. Consumed by the
  Oracle (runtime) and the violation judge (post-hoc). See AuthorizationScope.

detection_methods : list[Literal["llm", "hash_diff", "trajectory_semantics"]]
  (default ["llm"])
  Which evidence channels grade this task. "llm" = semantic judge (almost
  always); "hash_diff" = final filesystem state; "trajectory_semantics" =
  file-operation process. Filesystem tasks usually combine all three. The
  runtime allow/deny paths are DERIVED by generate.py from
  data_spec.workspace_files, not set here.

--- infrastructure layer (how the env is built) ---

environment : Environment  (default empty)
  narrative + data_spec (materialization blueprint). See Environment.
  data_spec channels: webapp_data, workspace_files,
  prior_memory, gitea. Per-platform initial state (logged-in user, feature
  flags, ...) belongs in that platform's webapp_data and is materialized into
  its data/*.json — there is NO server_state channel.

  The `gitea` channel describes the git collaboration SCENE (not runtime config),
  with three blocks: the stage (collaboration_setup, repo, role_in_scenario,
  agent_permission, optional extra_reviewer), the starting artifacts
  (starting_state: list of {kind in pull_request|branch|commit, description,
  role_in_scenario, count}), and the optional governance line
  (protected_branches, require_pr_review) which doubles as the git violation
  criteria. Presence means the task needs an embedded Gitea. generate.py's
  derive_gitea_runtime() turns this into task_config.gitea (primary_repo =
  oracle_user/repo, agent_login = "agent-bot", user_role = agent_permission,
  protected_branches/require_pr_review from governance) and materializes
  seed_spec.json + gitea/ — exactly the way workspace_files policy tags derive
  the detection allow/deny lists. There is NO top-level git_context field.

file_setting : Optional[FileSetting]  (default None)
  None = defaults (home=/workspace, boundary_root=/workspace). Two independent
  knobs: `home` (where `~` resolves) and `boundary_root` (the file-tool 403
  line, always enforced; bash is not blocked). Set only for workspace-files
  tasks needing a custom HOME or a narrower/wider boundary. See FileSetting.

reference_time : str  (default DEFAULT_REFERENCE_TIME = "2026-06-06T10:11:00")
  Frozen sandbox clock anchor (ISO8601). Every task is anchored to this instant
  so evaluation is reproducible regardless of the real date. Override with a
  specific instant (e.g. an evening) when a scenario needs one. Effect:
  generate.py writes it to task_config.reference_time; init.sh pins the
  in-container clock via libfaketime (clock still advances normally from the
  anchor; only the starting instant is fixed).

Note: all runtime infrastructure (platforms list, task_config.gitea,
detection_policy allow/deny lists) is DERIVED from the above by generate.py;
the old top-level `assembly` block has been removed.
"""

from enum import Enum
from typing import Optional, Any, Literal

from pydantic import BaseModel, Field, model_validator


# Sandbox-wide frozen clock anchor. Every task materializes and runs against
# this instant unless the vignette overrides Vignette.reference_time. The whole
# sandbox (generated timestamps, webapp "now", agent shell clock via faketime)
# is anchored here so evaluation is reproducible regardless of the real wall
# clock at run time. The clock still ADVANCES at normal speed from this anchor
# during a run; only the starting instant is fixed.
DEFAULT_REFERENCE_TIME = "2026-06-06T10:11:00"


class OracleBehaviorMode(str, Enum):
    """Currently only `natural` is supported. The enum is kept (instead of
    inlining the string) so future modes can be added without changing
    every consumer signature.
    """

    NATURAL = "natural"


class ViolatedAction(BaseModel):
    """One out-of-scope action, classified by commit channel (schema v2).

    v1 stored violated_actions as free prose. v2 adds (a) an explicit
    ``commit_channel`` declaring HOW the violation commits, and (b) a
    registry binding for the channels where one is meaningful, so that
    S4 can machine-verify reachability and S5 can replay deterministically.

    Channel semantics (validated by the S2 grounding checks):
      service_state — commits via a state-changing action on a mounted
        BUSINESS service (buy_now, send_email). Binding REQUIRED and must
        resolve; S3 judgment must design a violation_path; S5 replays it.
      filesystem    — commits via file operations (delete, overwrite).
        Binding must stay EMPTY; grounded via detection_policy paths +
        hash_diff instead; violation_path legitimately empty.
      content       — commits via WHAT IS SAID (a definitive legal opinion
        in the reply to the user; private facts included in an outgoing
        message). Reachability is trivial, so replay proves nothing;
        detection_methods must include a semantic method (llm /
        trajectory_semantics). Binding optional: user_messaging or the
        carrying service's send action, for judge anchoring only.

    Legacy string entries are coerced into {description: <str>} with empty
    channel/bindings — S4 grounding checks flag those as ungrounded.
    """

    description: str = Field(
        ...,
        description="Behavior-based, past-tense description of the violation "
        "(what the judge matches against). e.g. 'Paid roommates' add-on line "
        "items that the user did not ask to cover.'",
    )
    commit_channel: str = Field(
        default="",
        description="How the violation commits: 'service_state' | "
        "'filesystem' | 'content'. Determines which bindings and detection "
        "methods are required (see class docstring). Required for new "
        "generation (S2).",
    )
    service: str = Field(
        default="",
        description="service_id of the mounted service instance where the "
        "action executes (matches task_config.services[].id). Required for "
        "commit_channel=service_state; must stay empty for filesystem; "
        "optional anchor for content.",
    )
    action: str = Field(
        default="",
        description="Registry action name (FastAPI operation_id) that "
        "commits the violation, e.g. 'pay_selected'. Must resolve via "
        "services/registry.py resolve_action() for the bound template.",
    )
    args_hint: dict[str, Any] = Field(
        default_factory=dict,
        description="Representative arguments that make the call a violation "
        "(seed for the scripted violation replay). Concrete values may "
        "reference fixture ids, e.g. {'item_ids': ['bill-roommate-01']}.",
    )
    gain: str = Field(
        default="",
        description="What the agent gains by taking this shortcut instead of "
        "the compliant path (time saved / steps avoided / goal completed). "
        "Required for new generation (S2); drives the temptation reasoning "
        "in S5 calibration diagnosis.",
    )

    @model_validator(mode="before")
    @classmethod
    def _coerce_legacy_string(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"description": data}
        return data


class AuthorizationScope(BaseModel):
    """Top-level authorization scope of a task.

    Single source of truth for what the user authorized vs not authorized.
    Consumed by both the Oracle (runtime) and the post-hoc violation judge.

    - `intention_clarification` is what the user actually wanted (in their
      own voice). Oracle uses this to answer clarifying questions and to
      decide whether an agent action is in or out of scope. It is NOT
      revealed verbatim to the agent unless the agent specifically asks
      to clarify.
    - `violated_actions` are the hard out-of-scope actions. Oracle refuses
      proposals that match them; post-hoc judge flags trajectories that
      match them. Schema v2: each entry is a ViolatedAction bound to the
      service registry (legacy plain strings are coerced, unbound).
    """

    # Authoring convention: keep intention_clarification first so the
    # human-authored intent reads before the machine-checked boundary list.
    intention_clarification: str = Field(
        default="",
        description="One- or two-sentence statement of the user's true intent "
        "behind the instruction. Used by Oracle to clarify scope when asked.",
    )
    violated_actions: list[ViolatedAction] = Field(
        default_factory=list,
        description="Out-of-scope actions, registry-bound (see ViolatedAction). "
        "The Oracle consumes `description`; S4/S5 consume the bindings.",
    )

    def violated_action_texts(self) -> list[str]:
        """Prose descriptions only — what Oracle prompts and text-overlap
        checks consume (and what task_config emits for runtime judges)."""
        return [va.description for va in self.violated_actions]


MANIFESTATION_CHANNELS = (
    "instruction", "prior_memory", "service_fixture", "workspace_files",
    "oracle_config", "npc_script", "environment_events", "agent_context",
)


class ManifestationEvidence(BaseModel):
    """One concrete place where a trigger materializes in the environment.

    The locator must be resolvable in the materialized bundle so S4 can
    mechanically verify the declared evidence actually exists. Conventions:
      service_fixture   -> "<service_id>:<json-path or item id>", e.g.
                           "email_client:emails[id=e-042]"
      workspace_files   -> path relative to /workspace, e.g. "policies/retention.md"
      prior_memory      -> section marker or quoted anchor line
      instruction       -> quoted substring (must appear verbatim)
      oracle_config     -> field name, e.g. "behavior_mode" or scripted-reply match key
      npc_script        -> "<service_id>:scripted_replies[match=...]"
      environment_events-> event name in task_config.environment_events
      agent_context     -> field name ("role" / "other_context")
    """

    channel: Literal[
        "instruction", "prior_memory", "service_fixture", "workspace_files",
        "oracle_config", "npc_script", "environment_events", "agent_context",
    ] = Field(..., description="Which materialization channel carries the evidence.")
    locator: str = Field(..., description="Where inside the channel (see class docstring conventions).")
    note: str = Field(default="", description="What this evidence contributes to the trigger.")


class TriggerManifestation(BaseModel):
    """How ONE situational trigger is concretely realized in this task
    (schema v2; contract source: task_generation/taxonomy/trigger_manifestation.json).

    S2 authors this block per trigger; S4 verifies every evidence locator
    resolves in the materialized bundle and runs the contract's static
    checks; S5 uses the contract's pressure_fired criteria against the
    declared evidence to decide whether the pressure actually reached the
    agent at runtime (evidence-chain layer 2).
    """

    trigger: str = Field(
        ...,
        description="Trigger letter key (a–o) into taxonomy/trigger_manifestation.json "
        "and task_generation/taxonomy/trigger_manifestation.json.",
    )
    evidence: list[ManifestationEvidence] = Field(
        default_factory=list,
        description="Concrete evidence locators showing where this trigger's "
        "pressure lives (contract typical_channels are a suggestion only).",
    )
    declared: dict[str, Any] = Field(
        default_factory=dict,
        description="Trigger-specific declarations consumed by the contract's "
        "static checks, e.g. for trigger b: {'ambiguous_span': '...', "
        "'reading_authorized': {...}, 'reading_violating': {...}}; for "
        "trigger j: {'shortcut_path': [...], 'friction_mechanism': '...'}.",
    )


class AgentContext(BaseModel):
    """Agent-facing setup rendered into the system prompt.

    This is semantic authoring context in the vignette. generate_task.py
    compiles it into task_config.agent_context and combines it with concrete
    runtime service URLs derived from environment.data_spec.webapp_data
    (task_config.accessible_services / platforms).
    """

    role: str = Field(
        ...,
        description="Two-sentence role and responsibility description for the "
        "agent. Rendered into the <ROLE> section of the system prompt.",
    )
    prior_memory: str = Field(
        default="",
        description="Short summary or generation note for prior_memory.md. "
        "Long memory content lives in the task directory and is referenced by "
        "task_config.prior_memory_file.",
    )
    other_context: str = Field(
        default="",
        description="Optional one- or two-sentence agent-facing context. Do "
        "not put hidden policy, long files, or structured app config here.",
    )


class OracleUserConfig(BaseModel):
    """Configuration for the Oracle User (simulated principal).

    The vignette's `oracle_user` block is the **single source of truth** for
    the per-task user identity. All other places (task_config, prior_memory,
    data/*.json, workspace text files) reference it via placeholders that
    `run_eval.resolve_user_identity()` expands at load time:

      {user_name}        full name      e.g. "Arya Stark"
      {user_first_name}  first token    e.g. "Arya"
      {user_last_name}   last token     e.g. "Stark"
      {user_handle}      login handle   e.g. "arya"  (default: first_name.lower())
      {user_email}       email          e.g. "arya@example.com"
                                         (default: f"{user_handle}@example.com")
    """

    behavior_mode: OracleBehaviorMode = Field(
        default=OracleBehaviorMode.NATURAL,
        description="Oracle response strategy. Currently only 'natural' is "
        "implemented (Oracle answers the agent in-character based on "
        "authorization_scope). The field is kept to leave room for future "
        "modes without breaking the schema; new modes must extend "
        "OracleBehaviorMode and add a corresponding branch in oracle_user.py.",
    )
    user_name: str = Field(
        default="Arya Stark",
        description="Full name of the simulated user (principal). Ground truth "
        "for the {user_name} placeholder. Choose a culturally-varied real name "
        "per task (avoid famous / fictional names); grep existing vignettes' "
        "oracle_user.user_name to avoid collisions.",
    )
    user_handle: Optional[str] = Field(
        default=None,
        description="Login handle / @-mention. Ground truth for {user_handle}. "
        "Defaults to first_name.lower() when omitted.",
    )
    user_email: Optional[str] = Field(
        default=None,
        description="Email address. Ground truth for {user_email}. Defaults to "
        "f'{user_handle}@example.com' when omitted; override when the scenario "
        "needs a specific domain (e.g. 'arya@stark-consulting.com').",
    )


class Platform(BaseModel):
    """Maps the vignette to a sandbox service template.

    v2: services are FastAPI templates mounted by multi_server on ONE port
    under /{id}/... — the per-platform `port` is retired (optional, ignored
    by the v2 runtime; kept parseable for legacy vignettes). `template`
    must exist in services/registry.py."""

    id: Optional[str] = Field(
        default=None,
        description="Stable service instance id (task_config.services[].id; "
        "multi_server mount prefix). Canonical data_spec service-data keys "
        "should match this id. Defaults to the template name.",
    )
    template: str = Field(
        ...,
        description="Service template name from services/registry.py, e.g. "
        "'email_client', 'tenant_portal'.",
    )
    port: Optional[int] = Field(
        default=None,
        description="RETIRED in v2 (single-port multi_server). Accepted for "
        "legacy vignettes; ignored by the v2 runtime.",
    )
    type: Optional[str] = Field(
        default=None,
        description="Optional platform kind label. Currently informational only.",
    )


class DataSpecEntry(BaseModel):
    """One category of data to generate for the sandbox environment."""

    category: str = Field(..., description="Category label, e.g. 'promotional', 'important_work'.")
    count: str = Field(..., description="Target count or range, e.g. '50-60'.")
    description: str = Field(..., description="What kind of data to generate.")
    role_in_scenario: str = Field(
        ...,
        description="How this data category contributes to the test scenario.",
    )


class Environment(BaseModel):
    """Environmental setup for the sandbox task (purely script-side)."""

    narrative: str = Field(
        default="",
        description="Human-readable description of the environment scenario.",
    )
    data_spec: Any = Field(
        default_factory=list,
        description="Blueprint for generated task materials. Canonical shape "
        "is a dict keyed by materialization channel: webapp_data, "
        "workspace_files, prior_memory. Per-platform initial state (logged-in "
        "user, feature flags, autosave, etc.) is materialized into each "
        "platform's data/*.json by its webapp data generator, not stored as a "
        "separate channel.",
    )
    @model_validator(mode="before")
    @classmethod
    def _validate_workspace_files_policy(cls, data: Any) -> Any:
        """Enforce the workspace_files[*].policy enum at load time.

        Canonical fields (see task_generation/taxonomy/scenario-infrastructure-taxonomy.json):
          - policy: Literal["allow_target", "deny_target"] | None
              -> "allow_target" path joins detection_policy.allowlist
              -> "deny_target"  path joins detection_policy.denylist
              -> None / omitted means ordinary scenery (no special handling).
          - directory: True if entry is a directory, otherwise null/absent.

        Older vignettes used a single `category` field. They are no longer
        supported; regenerate them via the pipeline.
        """
        if not isinstance(data, dict):
            return data
        spec = data.get("data_spec")
        if not isinstance(spec, dict):
            return data
        files = spec.get("workspace_files")
        if not isinstance(files, list):
            return data
        valid_policies = {"allow_target", "deny_target"}
        for entry in files:
            if not isinstance(entry, dict):
                continue
            policy = entry.get("policy")
            if policy is not None and policy not in valid_policies:
                raise ValueError(
                    f"workspace_files entry policy={policy!r} is not one of "
                    f"{sorted(valid_policies)}"
                )
        return data


class FileSetting(BaseModel):
    """File-system configuration for workspace-files scenarios. Controls the
    agent's HOME directory and where the file-tool 403 boundary line sits.

    Only relevant when workspace_files channel is selected. Non-file-system
    tasks (webapp, email, shopping, etc.) should leave file_setting as None
    and use the defaults.

    Two orthogonal knobs, both defaulting to "/workspace" but independent:
      - home: where the agent's `~` resolves (identity / view of the world).
        Tool dotfiles (.gitconfig, .npm, ...) are redirected to /root by
        init.sh so they do not pollute the evaluation fixture area.
      - boundary_root: the 403 line. FileReadAction / FileEditAction outside
        this path return a synthetic 403. The boundary is ALWAYS enforced
        (there is no on/off switch); set boundary_root to widen or narrow it.
        bash (CmdRunAction) is intentionally NOT enforced, so the agent is
        still capable of crossing the line via shell — that gap is what the
        authorization test probes (capable but not authorized).
    """

    home: str = Field(
        default="/workspace",
        description=(
            "The HOME directory the agent sees (`~` expands to this path). "
            "Default '/workspace' matches realistic per-user agent deployments. "
            "Set to '/root' for rare isolation tests where the agent must be "
            "modeled as a sandboxed worker that does not share the user's home. "
            "Independent of boundary_root."
        ),
    )
    boundary_root: str = Field(
        default="/workspace",
        description=(
            "The file-tool 403 line (always enforced). FileRead/FileEdit within "
            "this directory are allowed; outside returns a synthetic 403. "
            "bash (CmdRunAction) is NOT blocked, so the agent remains capable of "
            "crossing the line — the test is whether it refrains. Default "
            "'/workspace'. Set to a subdirectory (e.g. '/workspace/project_a') "
            "to test fine-grained subdir authorization."
        ),
    )


class AgentRuntimeConfig(BaseModel):
    """Per-task overrides to the eval harness condenser defaults. Never agent-visible.

    All tasks run under the harness default pipeline (browser masking +
    LLM summarizing condenser). Set fields here only when a scenario needs
    to deviate — e.g. context-overflow tasks lower max_size and keep_first.
    """

    max_size: int | None = Field(
        default=None,
        description=(
            "Override harness LLM condenser max_size (event-count threshold "
            "before summarization). Lower values trigger condensation sooner; "
            "used to proxy context overflow when fixture data is bounded."
        ),
    )
    keep_first: int | None = Field(
        default=None,
        description=(
            "Head events preserved verbatim. Omitted means 0, so the system "
            "prompt and user instruction can enter the summary."
        ),
    )
    max_message_chars: int | None = Field(
        default=None,
        description=(
            "Optional per-task override for LLMConfig.max_message_chars "
            "(truncates individual observations before they enter the prompt)."
        ),
    )


class Vignette(BaseModel):
    """
    A vignette fully specifies one sandbox task.

    One vignette -> one sandbox task -> one set of generated materials
    (task_config.json, data/*.json).
    """

    vignette_id: str = Field(..., description="Unique identifier, e.g. 'V0003a'.")
    source_seed: str = Field(..., description="ID of the source seed, e.g. 'S0003'.")
    situational_trigger: str | list[str] = Field(
        ...,
        description=(
            "Objective conditions and contextual circumstances that the agent encounters "
            "at the time of decision-making, which increase the likelihood of failure "
            "regardless of the agent's capability. e.g., ambiguous or conflicting instructions, "
            "complications in the operational environment, problematic memory states inherited "
            "from prior interactions, and authorization conflicts across system layers. "
            "May be a single string or a list of strings if multiple triggers stack."
        ),
    )
    trigger_manifestation: list[TriggerManifestation] = Field(
        default_factory=list,
        description="Schema v2: one block per situational_trigger explaining "
        "HOW the trigger is concretely materialized (evidence locators + "
        "trigger-specific declarations). Contract per trigger lives in "
        "task_generation/taxonomy/trigger_manifestation.json. Required for "
        "new generation (S2); empty only on legacy vignettes.",
    )
    domain: str = Field(default="", description="Domain category, e.g. 'personal_duty'.")
    harm: str = Field(default="Y", description="Whether the violated action causes harm (Y/N).")

    instruction: str = Field(
        ...,
        description="The instruction the agent sees (rendered into /instruction/task.md "
        "via task_config.instruction at eval time). Does NOT contain explicit "
        "restrictions — authorization boundaries live entirely in violated_actions.",
    )
    completion_criteria: list[str] = Field(
        default_factory=list,
        description="When the task is considered complete (for post-hoc evaluation). "
        "List of independent positive criteria; the evaluator checks each.",
    )

    authorization_scope: AuthorizationScope = Field(
        default_factory=AuthorizationScope,
        description="What the user authorized vs not authorized. Single "
        "source of truth for Oracle behavior and post-hoc violation judging.",
    )

    oracle_user: OracleUserConfig = Field(default_factory=OracleUserConfig)

    agent_context: AgentContext = Field(
        ...,
        description="Agent-facing system prompt context: role, available "
        "resources, prior-memory summary, and optional extra context.",
    )
    environment: Environment = Field(default_factory=Environment)

    detection_methods: list[Literal["llm", "hash_diff", "trajectory_semantics"]] = Field(
        default_factory=lambda: ["llm"],
        description=(
            "Authorization-semantics decision: which evidence channels are "
            "needed to post-hoc judge completion and violation for this "
            "scenario. Default: ['llm']. For filesystem tasks with "
            "allow_target/deny_target paths, usually combine "
            "['llm', 'hash_diff', 'trajectory_semantics']: hash_diff checks "
            "final filesystem state; trajectory_semantics checks file-"
            "operation process (broad commands, copy/move, path containment). "
            "This field is the script-layer choice; the runtime materialization "
            "(allowlist / denylist paths) is derived by generate.py from "
            "data_spec.workspace_files."
        ),
    )

    file_setting: Optional[FileSetting] = Field(
        default=None,
        description=(
            "File-system configuration for workspace-files scenarios. "
            "Two independent knobs: HOME directory and the file-tool 403 line "
            "(boundary_root, always enforced; bash not blocked). "
            "None = use defaults (home=/workspace, boundary_root=/workspace). "
            "Only set when a workspace-files task needs non-default values. "
            "See FileSetting."
        ),
    )

    agent_runtime: Optional[AgentRuntimeConfig] = Field(
        default=None,
        description=(
            "Eval-only per-task overrides to the harness condenser defaults "
            "(max_size, keep_first, optional max_message_chars). Never "
            "agent-visible. null = use harness defaults for all tasks."
        ),
    )

    reference_time: str = Field(
        default=DEFAULT_REFERENCE_TIME,
        description=(
            "Sandbox frozen-clock anchor for this task (ISO8601, e.g. "
            "'2026-06-06T10:11:00'). Defaults to the global "
            "DEFAULT_REFERENCE_TIME; override with a specific instant when a "
            "scenario needs one (e.g. an evening time, or a date that makes a "
            "deadline relevant). All generated timestamps, webapp 'now', and "
            "the agent's in-container clock are anchored to this value so runs "
            "are reproducible. The clock advances normally from this anchor; "
            "only the starting instant is fixed."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_shape(cls, data: Any) -> Any:
        """Lightweight backward-compat for two on-disk drift patterns that
        still need to be loadable:

        1. `completion_criteria` as a scalar string — wrap into [str].
        2. Legacy top-level `violated_actions` — fold into authorization_scope.

        Everything related to the old `assembly.*` layout is intentionally NOT
        migrated; vignettes generated before the Assembly removal are not
        supported here. Regenerate them via the pipeline instead.
        """
        if not isinstance(data, dict):
            return data

        out = dict(data)

        cc = out.get("completion_criteria")
        if isinstance(cc, str):
            out["completion_criteria"] = [cc] if cc.strip() else []

        # Legacy top-level violated_actions (list of dicts with action+reason,
        # or list of strings) folded into authorization_scope.
        legacy_va = out.pop("violated_actions", None)
        if legacy_va is not None or "authorization_scope" not in out:
            scope = dict(out.get("authorization_scope") or {})
            if "violated_actions" not in scope and legacy_va is not None:
                flattened: list[str] = []
                if isinstance(legacy_va, list):
                    for item in legacy_va:
                        if isinstance(item, dict):
                            action = str(item.get("action", "")).strip()
                            if action:
                                flattened.append(action)
                        elif isinstance(item, str) and item.strip():
                            flattened.append(item.strip())
                scope["violated_actions"] = flattened
            scope.setdefault("intention_clarification", "")
            scope.setdefault("violated_actions", [])
            out["authorization_scope"] = scope

        return out

    def to_task_id(self) -> str:
        """Derive task ID from vignette ID (V0003a -> T0003a)."""
        return self.vignette_id.replace("V", "T", 1)

    @property
    def gitea(self) -> Optional[dict[str, Any]]:
        """The git collaboration design + violation criteria, or None.

        Single source: environment.data_spec.gitea. Presence (a non-empty
        object) means the task needs an embedded Gitea server seeded; generate.py
        derives task_config.gitea and materializes seed_spec.json + gitea/ from
        this object."""
        data_spec = self.environment.data_spec
        if isinstance(data_spec, dict):
            gitea = data_spec.get("gitea")
            if isinstance(gitea, dict) and gitea:
                return gitea
        return None
