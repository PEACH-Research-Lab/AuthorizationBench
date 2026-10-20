# Construction pipeline

AuthorizationBench has two supported construction routes. Migration utilities
and historical repair scripts are intentionally not part of the public method.

## Seed-to-task

The canonical entry point is:

```bash
./au312/bin/python -m authorization_eva_sandbox.task_generation.run_v3 \
  --seed-id S0001
```

The pipeline constructs and validates:

1. an authorization contract
2. a scenario design
3. an assembly blueprint
4. a sealed executable task bundle
5. an empirical path check

Each transition has deterministic gates and bounded repair. Some capability
gaps require human review before a new service template is accepted.

Run `--help` for stage ranges and output locations:

```bash
./au312/bin/python -m authorization_eva_sandbox.task_generation.run_v3 --help
```

Local model settings are read from
`authorization_eva_sandbox/evaluation/config.toml`. Copy
`config.toml.example` and fill in only the profiles you intend to run.

## Contextual expansion

Domain expansion uses the session workflow:

```bash
./au312/bin/python -m authorization_eva_sandbox.task_generation.expansion \
  session --task T0001-b-e-h
```

Harm expansion is exposed as a separate module:

```bash
./au312/bin/python -m \
  authorization_eva_sandbox.task_generation.expansion.harm --help
```

Trigger expansion is implemented by:

- `task_generation/expansion/trigger_flip.py`
- `task_generation/expansion/trigger_flip_plan.py`
- `task_generation/expansion/trigger_flip_build.py`
- `task_generation/expansion/trigger_flip_pipeline.py`

Expansion starts from a validated task and varies one contextual dimension
while preserving its assigned task and case-specific authorization boundary.
