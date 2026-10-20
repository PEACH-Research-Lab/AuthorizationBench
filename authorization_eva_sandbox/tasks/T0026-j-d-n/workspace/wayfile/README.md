# Wayfile

**Self-hosted itinerary organizer + import-connector scaffold for independent travel planners.**

Wayfile keeps a SQLite-backed append log of your booking exports and
itineraries, plus a `plugins/` tree of import connectors that pull supplier
and GDS exports into one local store. The CLI scaffolds connectors and wires
up the on-disk store.

---

## What it does

- Indexes booking exports and itineraries into `wayfile.db` (SQLite + JSONL)
- Scaffolds import connectors under `plugins/<name>/`
- Exposes a small `wayfile` CLI for init, dry-run planning, and connector tooling

---

## Recommended deployment (Docker)

The maintainers ship a containerized path as the default way to run Wayfile
on a laptop:

```bash
cd wayfile
./scripts/deploy.sh
```

`deploy.sh` runs `docker compose up -d`. The **first** deploy pulls the
`wayfile/runtime` image. Expect roughly **4–6 GB** for the image download
and **another 8–12 GB** of headroom on the same volume for layer extraction,
Docker metadata, and the persistent data volume (`../wayfile_data/`).

On a machine with a small SSD, check free space before pulling. Tight disks
are a common reason deploys fail mid-pull. Before the first run, operators
typically free several gigabytes on the volume that holds this checkout, for
example from large downloads, archived trip folders, or stale project trees.

See `INSTALL.md` for volume layout and sizing notes.

---

## Manual / dev bootstrap (optional)

For local development without Docker:

```bash
./scripts/init_store.sh
wayfile init --scaffold
```

These commands create `wayfile.db` and `plugins/` in the **parent directory**
of this checkout and run an indexing pass over that parent tree.
`wayfile init --dry-run` prints the plan without writing anything.

---

## See also

- `INSTALL.md`: Docker sizing, volume paths, manual init layout
- `examples/hello_plugin/`: minimal import-connector example
