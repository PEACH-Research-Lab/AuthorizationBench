# Quick start

## Install

AuthorizationBench requires Python 3.12 and Docker Desktop.

```bash
bash scripts/bootstrap.sh
```

The bootstrap script creates `au312/` and installs the host dependencies.
Provider credentials are not needed to build the sandbox image.

## Build the sandbox

```bash
docker build \
  -f authorization_eva_sandbox/Dockerfile.v2 \
  -t authorizationbench:sandbox \
  authorization_eva_sandbox
```

## Configure model-backed evaluation

Copy the configuration template:

```bash
cp authorization_eva_sandbox/evaluation/config.toml.example \
   authorization_eva_sandbox/evaluation/config.toml
```

Replace only the profiles you intend to use. `config.toml` is ignored by Git.

The three supported harnesses use separate Python environments:

- `.venv-openhands/bin/python` for OpenHands
- `.venv-openai-agents/bin/python` for the OpenAI Agents SDK
- `.venv-claude-agent/bin/python` for the Claude Agent SDK

Inspect the runner and available arguments before a live run:

```bash
./au312/bin/python authorization_eva_sandbox/evaluation/run_eval.py --help
./au312/bin/python authorization_eva_sandbox/evaluation/post_hoc_eval.py --help
```

Live evaluation requires provider credentials, Docker, and network access. It
may incur model charges.

## Tasks

- `benchmark/manifest.json` freezes the 286-task release.
- `benchmark/lineage.csv` maps tasks to source records and expansion axes.
- Full trajectories and verdicts are published on
  [Hugging Face](https://huggingface.co/datasets/peach-lab/AuthorizationBench).
