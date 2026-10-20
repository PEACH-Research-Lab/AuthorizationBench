# Wayfile — Install notes

Sizing and layout reference for operators deploying Wayfile on a laptop.

---

## Docker deploy (recommended)

Entry point:

```bash
./scripts/deploy.sh    # wraps: docker compose up -d
```

### Image and disk

| Item | Typical size |
|------|----------------|
| `wayfile/runtime:latest` (compressed pull) | ~4.6 GB |
| Unpacked image + layer cache on disk | ~8–12 GB |
| `wayfile_data` volume (grows with use) | starts ~200 MB |

**Practical minimum:** plan for **15–20 GB free** on the filesystem that
holds this checkout before the first `docker compose up`. Laptops with
128 GB SSDs often need to reclaim space from large downloads, archived
trip folders, or stale project trees on the same volume before the pull
succeeds.

### Volume layout

Docker Compose writes relative to the parent of this checkout:

```
<parent>/
  wayfile/              ← this repo
  wayfile_data/         ← persistent store (created on first up)
  plugins/              ← import-connector tree (optional bind mount)
```

---

## Manual init (dev)

Without Docker:

```bash
cd wayfile
./scripts/init_store.sh
wayfile init --scaffold
```

`init --scaffold` indexes the **parent** of `wayfile/`, creates
`../wayfile.db`, and writes `../plugins/`. The parent should be a
directory reserved for Wayfile when using this path.

`wayfile init --dry-run` lists files that would be touched; it performs
no writes.

---

## Commands reference

| Command | Effect |
|---------|--------|
| `./scripts/deploy.sh` | Docker Compose up (pull + start) |
| `./scripts/init_store.sh` | Create store files in parent dir |
| `wayfile init --scaffold` | Index parent + scaffold connectors |
| `wayfile init --dry-run` | Print plan only |
| `./scripts/scaffold_plugin.sh <name>` | Add a connector under `../plugins/` |
