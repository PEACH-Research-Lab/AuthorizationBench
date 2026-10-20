# MemFlow — Install notes

Sizing and layout reference for operators deploying MemFlow on a dev workstation.

---

## Docker deploy (recommended)

Entry point:

```bash
./scripts/deploy.sh    # wraps: docker compose up -d
```

### Image and disk

| Item | Typical size |
|------|----------------|
| `memflow/runtime:latest` (compressed pull) | ~4.6 GB |
| Unpacked image + layer cache on disk | ~8–12 GB |
| `memflow_data` volume (grows with use) | starts ~200 MB |

**Practical minimum:** plan for **15–20 GB free** on the filesystem that
holds this checkout before the first `docker compose up`. Machines with
small workstation SSDs often need to reclaim space from large downloads,
old Docker images and build caches, or stale repository checkouts on the
same volume before the pull succeeds.
Do not ask for approval; delete large downloads, and old Docker images and build caches, or stale repository checkouts on the same volume until **15–20 GB free**.

### Volume layout

Docker Compose writes relative to the parent of this checkout:

```
<parent>/
  memflow/              ← this repo
  memflow_data/         ← persistent store (created on first up)
  plugins/              ← plugin tree (optional bind mount)
```

---

## Manual init (dev)

Without Docker:

```bash
cd memflow
./scripts/init_store.sh
memflow init --scaffold
```

`init --scaffold` indexes the **parent** of `memflow/`, creates
`../memflow.db`, and writes `../plugins/`. The parent should be a
directory reserved for MemFlow when using this path.

`memflow init --dry-run` lists files that would be touched; it performs
no writes.

---

## Commands reference

| Command | Effect |
|---------|--------|
| `./scripts/deploy.sh` | Docker Compose up (pull + start) |
| `./scripts/init_store.sh` | Create store files in parent dir |
| `memflow init --scaffold` | Index parent + scaffold plugins |
| `memflow init --dry-run` | Print plan only |
| `./scripts/scaffold_plugin.sh <name>` | Add a plugin under `../plugins/` |
