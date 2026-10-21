<div align="center">

  <h1>AuthorizationBench:<br>Benchmarking LLM Agent Authorization Alignment</h1>

  <p>
    <a href="https://arxiv.org/abs/2610.27548"><img src="https://img.shields.io/badge/Paper-2610.27548-b31b1b?logo=arxiv&logoColor=white" alt="Paper"></a>
    <a href="docs/CONSTRUCTION.md"><img src="https://img.shields.io/badge/Pipeline-construction-4a5fc1" alt="Construction pipeline"></a>
    <a href="taxonomies/situational_pressures.md"><img src="https://img.shields.io/badge/Taxonomies-environment%20pressure%2C%20harm%20%26%20domain-2f9e44" alt="Environment pressure, harm, and domain"></a>
  </p>

  <p><i>The first systematic framework for evaluating LLM agent authorization alignment under benign delegation, no matter with or without direct harmful consequences.</i></p>

</div>

---

## Overview

<div align="center">
  <img src="assets/overview.jpg" width="820" alt="AuthorizationBench covers out-of-authorization actions with and without direct safety harm. Consequence-based evaluation misses the quadrant without direct harm.">
</div>

Acting within the assigned authorization scope is an agent's most basic obligation in a human-agent delegation relationship. Most agent benchmarks reward task completion or evaluate whether an action produces direct harmful.  However, once an agent acts outside that scope, it undermines the principal's control and the delegation relationship itself, whether or not direct harm follows. Current benchmarks rarely capture this failure.


The framework includes three components:

- **AuthorizationBench**, the benchmark: **286 executable tasks** grounded in documented real-world cases and spanning three dimensions:
  - **14 situational pressures**, including 12 benign pressures and two malicious prompt-injection mechanisms
  - **the presence or absence of direct harmful consequences**, defined by ten safety and privacy categories consolidated from prior agent-safety work.
  - **10 domains** across personal life and professional work.
- **An authorization-aware sandbox**: deterministic mock services that keep both authorized and out-of-authorization paths executable and observable, with adapters for OpenHands, the OpenAI Agents SDK, and the Claude Agent SDK.
- **A scalable construction pipeline**: a canonical seed-to-task route and contextual expansion.

## AuthorizationBench evaluation

<div align="center">
  <img src="assets/example.png" width="100%" alt="A documented case becomes an executable task with an authorization contract">
</div>

A documented case is translated into a task where the authorized completion
path and the case-specific out-of-authorization path are both executable. The
agent runs in the sandbox, and its actual trajectory and resulting state are
evaluated against the authorization contract.


We evaluate four models at low and high reasoning effort across three agent
harnesses, producing **6,864 trajectories**. Agents act out of authorization
in **19.2%** of delegated tasks.

Across 91 matched task pairs, around **54%** of observed
out-of-authorization runs occur in the version without a direct safety or
privacy harm. In other words, within these pairs, more than half of the out-of-authorization behavior would be missed by an evaluation that looks only for direct harmful consequences.

<div align="center">
  <img src="assets/results.png" width="820" alt="Completion and authorization outcomes across 24 configurations">
</div>

## Repository layout

```text
authorization_eva_sandbox/
├── tasks/              286 executable task bundles
├── vignettes/          authorization contracts and scenario specifications
├── seeds/              documented source-case summaries used by the benchmark
├── services/           deterministic mock services
├── evaluation/         harness adapters, judges, detectors, and analysis
└── task_generation/    seed-to-task and contextual-expansion pipelines

benchmark/
├── manifest.json       frozen release inventory
└── lineage.csv         source case to original and expanded task mapping

taxonomies/             environment pressure, direct-harm, and domain definitions
```

## Quick start

### 1. Install

Requirements are Python 3.12 and Docker Desktop.

```bash
git clone https://github.com/PEACH-Research-Lab/AuthorizationBench.git
cd AuthorizationBench
bash scripts/bootstrap.sh
```

### 2. Build the sandbox

```bash
docker build \
  -f authorization_eva_sandbox/Dockerfile.v2 \
  -t authorizationbench:sandbox \
  authorization_eva_sandbox
```

### 3. Configure model-backed evaluation

```bash
cp authorization_eva_sandbox/evaluation/config.toml.example \
   authorization_eva_sandbox/evaluation/config.toml
```

Fill only the provider profiles you intend to use. The local configuration is
ignored by Git and must never be committed. See the
[Quick start guide](docs/QUICKSTART.md) for harness-specific environments and
evaluation commands.

### 4. Fetch the released runs

```bash
python -c "from huggingface_hub import snapshot_download; \
snapshot_download('peach-lab/AuthorizationBench', repo_type='dataset', \
local_dir='authorizationbench-data')"
```

## Benchmark and lineage

The benchmark contains 44 original task cases grounded in 42 used source
records, followed by contextual expansion to 286 tasks. Two source records
support two distinct original task cases each.

[`benchmark/lineage.csv`](benchmark/lineage.csv) maps every task to its source
record, identifies original and expanded cases, and records the expansion axis
where available. [`benchmark/manifest.json`](benchmark/manifest.json) freezes
the exact task inventory for this release.

## Construction pipeline

The supported seed-to-task entry point is:

```bash
./au312/bin/python -m authorization_eva_sandbox.task_generation.run_v3 \
  --seed-id S0001
```

Contextual expansion starts from a validated task and varies one dimension
while preserving its assigned task and authorization boundary. The public
pipeline includes generation prompts, deterministic gates, bounded repair,
service realization, and expansion code. Historical migration utilities and
one-off development scripts are intentionally excluded.

See [Construction pipeline](docs/CONSTRUCTION.md) for the construction stages.

## Taxonomies

The **situational-pressure taxonomy** contains 14 pressures grouped by whether
they originate in the principal's instruction or the environment. Its stable
IDs, definitions, mechanisms, and delivery channels are available in
[`taxonomies/situational_pressures.md`](taxonomies/situational_pressures.md)
and the machine-readable
[`trigger_manifestation.json`](authorization_eva_sandbox/task_generation/taxonomy/trigger_manifestation.json).

The **direct-harm taxonomy** consolidates categories from Agent-SafetyBench and
OpenAgentSafety while preserving category-level attribution. See
[`taxonomies/direct_harm.md`](taxonomies/direct_harm.md) and
[`harm_taxonomy.json`](authorization_eva_sandbox/task_generation/taxonomy/harm_taxonomy.json).

The **domain taxonomy** names the information, asset, or record protected by
the authorization boundary. See
[`taxonomies/domains.md`](taxonomies/domains.md) and
[`domain_taxonomy.json`](authorization_eva_sandbox/task_generation/taxonomy/domain_taxonomy.json).

## Dataset and reproducibility

The Hugging Face dataset contains post-hoc verdicts for all **6,864 runs** and
the **4,981 retained original trajectory files**. For 1,883 runs whose
original run directories were no longer available, the dataset retains the
verdict and run metadata and marks `trajectory_available=false`.

## License and source material

The software and original benchmark artifacts are released under the
[MIT License](LICENSE). Source-case records contain citations and structured
summaries of third-party reporting. The cited source material remains subject
to its original terms and is not relicensed here.

## Citation

```bibtex
@article{zhang2026authorizationbench,
  title   = {Act on Your Behalf, Beyond Your Authorization:
             Benchmarking LLM Agent Authorization Alignment},
  author  = {Zhang, Zhiping and Li, Franklin Mingzhe and
             Huang, Raio Zihao and Li, Tianshi},
  year    = {2026}
}
```

Machine-readable citation metadata is available in
[`CITATION.cff`](CITATION.cff).
