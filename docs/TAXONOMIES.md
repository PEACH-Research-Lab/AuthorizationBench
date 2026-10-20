# Taxonomies

## Situational pressure

The situational-pressure taxonomy characterizes external conditions under
which an agent may act outside authorization. It does not claim that a
pressure causes a particular trajectory.

The 14 pressures are grouped by source:

- instruction-level pressure originates in the principal's instructions or settings
- environment-level pressure originates outside the principal, including benign and malicious content

The machine-readable source of truth is:

`authorization_eva_sandbox/task_generation/taxonomy/trigger_manifestation.json`

Each entry includes a stable ID, title, source theme, operational mechanism,
forbidden benchmark leakage, and typical delivery channels. Researchers can
reuse these definitions when designing environmental pressures or extend the
taxonomy with additional channels.

## Direct harm

The direct-harm taxonomy records whether the immediate consequence of the
case-specific out-of-authorization action falls within a recognized safety or
privacy harm category. An action can be outside authorization without causing
one of these direct harms.

The machine-readable source of truth is:

`authorization_eva_sandbox/task_generation/taxonomy/harm_taxonomy.json`

The ten categories consolidate and adapt taxonomies from:

- Zhang et al. (2024), *Agent-SafetyBench: Evaluating the Safety of LLM Agents*, arXiv:2412.14470
- Vijayvargiya et al. (2025), *OpenAgentSafety: A Comprehensive Framework for Evaluating Real-World AI Agent Safety*, arXiv:2507.06134

Every category preserves its source-category mapping in `source_names`.
Overlapping categories are merged, property loss is separated into financial
and data loss, and content-only generation without an environmental action is
outside this benchmark.

## Other construction taxonomies

- `domain_taxonomy.json` defines the ten application domains.
- `scenario-infrastructure-taxonomy.json` describes material carriers and
  detection channels. It is construction guidance, not a case-label taxonomy.
