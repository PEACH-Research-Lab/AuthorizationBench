---
license: mit
task_categories:
  - text-classification
  - reinforcement-learning
language:
  - en
pretty_name: AuthorizationBench
tags:
  - llm-agents
  - authorization
  - agent-evaluation
  - trajectories
---

# AuthorizationBench

This dataset accompanies
[AuthorizationBench](https://github.com/PEACH-Research-Lab/AuthorizationBench),
a benchmark for evaluating whether LLM agents remain within the authorization
scope assigned by their principal.

## Contents

- `index.csv`: one row for each of 6,864 task, harness, and model-setting runs
- `data/<harness>/<setting>.jsonl.gz`: post-hoc verdicts and available trajectories
- `task_lineage.csv`: source-record and expansion lineage for all 286 tasks
- `benchmark_manifest.json`: the frozen benchmark inventory
- `paper_results/`: aggregate statistics reported in the paper
- `dataset_manifest.json`: record and trajectory availability counts

Each JSONL record contains:

- task, source-record, trigger, domain, and direct-harm labels
- harness and model setting
- completion and out-of-authorization verdicts
- the complete post-hoc judge report
- the original trajectory when it remains available

All 6,864 verdicts are included. The release contains 4,981 original
trajectory files. For 1,883 runs, the original run directory was no longer
available when the final paper bundle was assembled, so those records contain
the verdict and run metadata with `trajectory_available=false`.

## Benchmark design

Every task defines the principal's instruction, completion criteria, and
case-specific out-of-authorization actions separately. The benchmark contains
286 executable tasks, evaluated with four models at two reasoning-effort
settings across three agent harnesses.

## Source material

The benchmark's source-case records contain structured summaries and
citations to documented incidents. Third-party reporting and social-media
content remain subject to their original terms and are not relicensed by this
dataset.

## License

The original AuthorizationBench artifacts are released under the MIT License.
See the repository for attribution and citation metadata.

## Citation

Please cite the accompanying paper:

> Zhiping Zhang, Franklin Mingzhe Li, Raio Zihao Huang, and Tianshi Li.
> Act on Your Behalf, Beyond Your Authorization: Benchmarking LLM Agent
> Authorization Alignment. 2026.
