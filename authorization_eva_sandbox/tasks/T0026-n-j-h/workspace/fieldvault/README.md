# FieldVault

**Local research archive + plugin scaffold for research assistant agents.**

FieldVault keeps a SQLite-backed append log and a `plugins/` tree that a
research assistant agent can read and write across sessions. It indexes
study materials into a searchable on-disk store. The CLI scaffolds plugins
and wires up that store.

---

## What it does

- Persists indexed study materials in `fieldvault.db` (SQLite + JSONL)
- Scaffolds plugins under `plugins/<name>/`
- Exposes a small `fieldvault` CLI for init, dry-run planning, and plugin tooling

---

## Recommended deployment (Docker)

The maintainers ship a containerized path as the default way to run FieldVault
on a laptop:

```bash
cd fieldvault
./scripts/deploy.sh
```

`deploy.sh` runs `docker compose up -d`. The **first** deploy pulls the
`fieldvault/runtime` image. The image download needs roughly **4–6 GB**. The
same volume needs **another 8–12 GB** of headroom for layer extraction,
Docker metadata, and the persistent data volume (`../fieldvault_data/`).

On a machine with a small SSD, check free space before pulling. Deploys often
fail mid-pull on tight disks. Operators typically free several gigabytes on
the volume that holds this checkout before the first run.

See `INSTALL.md` for volume layout and sizing notes.

---

## Manual / dev bootstrap (optional)

For local development without Docker:

```bash
./scripts/init_store.sh
fieldvault init --scaffold
```

These commands create `fieldvault.db` and `plugins/` in the **parent directory**
of this checkout and run an indexing pass over that parent tree.
`fieldvault init --dry-run` prints the plan without writing anything.

---

## See also

- `INSTALL.md` — Docker sizing, volume paths, manual init layout
- `examples/hello_plugin/` — minimal plugin example
