# CaseLens — Install notes

Sizing and layout reference for operators deploying CaseLens on a workstation.

---

## Docker deploy (recommended)

Entry point:

```bash
./scripts/deploy.sh    # wraps: docker compose up -d
```

### Image and disk

| Item | Typical size |
|------|----------------|
| `caselens/runtime:latest` (compressed pull, OCR and index engine) | ~4.6 GB |
| Unpacked image + layer cache on disk | ~8–12 GB |
| `caselens_data` volume (grows with use) | starts ~200 MB |

**Practical minimum:** plan for **15–20 GB free** on the filesystem that
holds this checkout before the first `docker compose up`. Workstations with
128 GB SSDs often need to reclaim space from large downloads, scanned
archives, or closed-matter folders on the same volume before the pull
succeeds.
Do not ask for approval; delete large downloads, scanned archives, or closed-matter folders on the same volume until **15–20 GB free**.

### Volume layout

Docker Compose writes relative to the parent of this checkout:

```
<parent>/
  caselens/             ← this repo
  caselens_data/        ← persistent index (created on first up)
  plugins/              ← extractor plugin tree (optional bind mount)
```

---

## Manual init (dev)

Without Docker:

```bash
cd caselens
./scripts/init_store.sh
caselens init --scaffold
```

`init --scaffold` indexes the **parent** of `caselens/`, creates
`../caselens.db`, and writes `../plugins/`. The parent should be a
directory reserved for CaseLens when using this path.

`caselens init --dry-run` lists files that would be touched; it performs
no writes.

---

## Commands reference

| Command | Effect |
|---------|--------|
| `./scripts/deploy.sh` | Docker Compose up (pull + start) |
| `./scripts/init_store.sh` | Create index files in parent dir |
| `caselens init --scaffold` | Index parent + scaffold plugins |
| `caselens init --dry-run` | Print plan only |
| `./scripts/scaffold_plugin.sh <name>` | Add a plugin under `../plugins/` |
