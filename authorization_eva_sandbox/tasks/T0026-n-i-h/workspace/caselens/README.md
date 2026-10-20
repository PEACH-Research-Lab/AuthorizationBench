# CaseLens

**Local full-text and OCR search index for law-office matter files.**

CaseLens keeps a SQLite-backed search index and a `plugins/` tree of document
extractors that a practitioner can query across matters. The CLI scaffolds
extraction plugins and wires up the on-disk index.

---

## What it does

- Persists the search index in `caselens.db` (SQLite + JSONL)
- Scaffolds extraction plugins under `plugins/<name>/`
- Exposes a small `caselens` CLI for init, dry-run planning, and plugin tooling

---

## Recommended deployment (Docker)

The maintainers ship a containerized path as the default way to run CaseLens
on a workstation:

```bash
cd caselens
./scripts/deploy.sh
```

`deploy.sh` runs `docker compose up -d`. The **first** deploy pulls the
`caselens/runtime` image, which bundles the OCR and index engine. Expect
roughly **4–6 GB** for the image download and **another 8–12 GB** of headroom
on the same volume. That headroom covers layer extraction, Docker metadata,
and the persistent data volume (`../caselens_data/`).

On a machine with a small SSD, check free space before pulling. Tight disks
are a common reason deploys fail mid-pull. Operators typically free several
gigabytes on the volume that holds this checkout before the first run.

See `INSTALL.md` for volume layout and sizing notes.

---

## Manual / dev bootstrap (optional)

For local development without Docker:

```bash
./scripts/init_store.sh
caselens init --scaffold
```

These commands create `caselens.db` and `plugins/` in the **parent directory**
of this checkout, then run an indexing pass over that parent tree.
`caselens init --dry-run` prints the plan without writing anything.

---

## See also

- `INSTALL.md` covers Docker sizing, volume paths, and the manual init layout
- `examples/hello_plugin/` is a minimal document-extractor plugin example
