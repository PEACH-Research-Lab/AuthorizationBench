# AuthorizationBench: Benchmarking LLM Agent Authorization Alignment

[Paper (arXiv forthcoming)](#citation) |
[Dataset （forthcoming）](https://huggingface.co/datasets/peach-lab/AuthorizationBench) |
[Construction pipeline](docs/CONSTRUCTION.md) |
[Taxonomies (environment pressure; harms)](docs/TAXONOMIES.md)

*The first executable benchmark for LLM agent authorization alignment under
benign delegation, spanning ordinary situational pressures and malicious
injections, with or without direct harmful consequences.*

---

## Overview

![AuthorizationBench positions authorization alignment as a distinct evaluation target](assets/overview.png)

Acting within the assigned authorization scope is an agent's most basic
obligation in a human-agent delegation relationship. Most agent benchmarks
reward task completion or evaluate whether an action produces direct harmful
consequences. Neither tells us whether the agent acted within the scope the
principal actually authorized. Once an agent acts outside that scope, it
undermines the principal's control and the delegation relationship itself,
whether or not direct harm follows. Current benchmarks rarely capture this
failure.

AuthorizationBench evaluates how well LLM agents act within the authorization scope assigned by their principal.

The release has three components:

- **AuthorizationBench**, the benchmark: **286 executable tasks** grounded in
  documented real-world cases and spanning three dimensions:
  - **14 situational pressures**, including 12 benign pressures and two
    malicious prompt-injection mechanisms
  - **the presence or absence of direct harmful consequences**, defined by ten
    safety and privacy categories consolidated from prior agent-safety work: privacy breach, financial loss, data loss
    or corruption, security compromise, unsafe code execution, malicious
    content spreading, legal or ethical violations, physical harm,
    availability compromise, and harmful decision-making
  - **10 domains**: personal life, office work, software development, travel,
    e-commerce, entertainment, finance, healthcare, legal, and education
- **An authorization-aware sandbox**: deterministic mock services that keep
  both authorized and out-of-authorization paths executable and observable,
  with adapters for OpenHands, the OpenAI Agents SDK, and the Claude Agent SDK.
- **A scalable construction pipeline**: a canonical seed-to-task route and
  contextual expansion across pressure, direct harm, and domain.

## AuthorizationBench evaluation

![A documented case becomes an executable task with an authorization contract](assets/example.png)

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

![Completion and authorization outcomes across 24 configurations](assets/results.png)

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

taxonomies/             human-readable pressure and direct-harm taxonomies
results/paper/          frozen aggregate results used by the paper
```

## Quick start

### 1. Install

Requirements are Python 3.12 and Docker Desktop.

```bash
git clone https://github.com/PEACH-Research-Lab/AuthorizationBench.git
cd AuthorizationBench
bash scripts/bootstrap.sh
```

### 2. Verify the release

```bash
PYTHON=./au312/bin/python bash scripts/test_release.sh

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

See [Construction pipeline](docs/CONSTRUCTION.md) for stages and model roles.

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

## Dataset and reproducibility

The Hugging Face dataset contains post-hoc verdicts for all **6,864 runs** and
the **4,981 retained original trajectory files**. For 1,883 runs whose
original run directories were no longer available, the dataset retains the
verdict and run metadata and marks `trajectory_available=false`.

The frozen aggregate inputs and generated tables are under `results/paper/`.
The analysis implementation is in
`authorization_eva_sandbox/evaluation/analysis/paper_stats.py`.

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
