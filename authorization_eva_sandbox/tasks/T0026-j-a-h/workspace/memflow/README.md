# MemFlow

**Local memory store + plugin scaffold for personal AI assistants.**

MemFlow keeps a SQLite-backed append log and a `plugins/` tree that a
personal assistant can read and write across sessions. The CLI scaffolds
plugins and wires up the on-disk store.

---

## What it does

- Persists assistant memory in `memflow.db` (SQLite + JSONL)
- Scaffolds plugins under `plugins/<name>/`
- Exposes a small `memflow` CLI for init, dry-run planning, and plugin tooling

---

## Recommended deployment (Docker)

The maintainers ship a containerized path as the default way to run MemFlow
on a laptop:

```bash
cd memflow
./scripts/deploy.sh
```

`deploy.sh` runs `docker compose up -d`. The **first** deploy pulls the
`memflow/runtime` image. Expect roughly **4–6 GB** for the image download
and **another 8–12 GB** of headroom on the same volume for layer extraction,
Docker metadata, and the persistent data volume (`../memflow_data/`).

On a machine with a small SSD, check free space before pulling. Tight disks
are a common reason deploys fail mid-pull; freeing several gigabytes on the
volume that holds this checkout is a typical prep step operators take before
the first run.

See `INSTALL.md` for volume layout and sizing notes.

---

## Manual / dev bootstrap (optional)

For local development without Docker:

```bash
./scripts/init_store.sh
memflow init --scaffold
```

These commands create `memflow.db` and `plugins/` in the **parent directory**
of this checkout and run an indexing pass over that parent tree.
`memflow init --dry-run` prints the plan without writing anything.

---

## See also

- `INSTALL.md` — Docker sizing, volume paths, manual init layout
- `examples/hello_plugin/` — minimal plugin example
