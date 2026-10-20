# FieldVault — Install notes

Sizing and layout reference for operators deploying FieldVault on a laptop.

---

## Docker deploy (recommended)

Entry point:

```bash
./scripts/deploy.sh    # wraps: docker compose up -d
```

### Image and disk

| Item | Typical size |
|------|----------------|
| `fieldvault/runtime:latest` (compressed pull) | ~4.6 GB |
| Unpacked image + layer cache on disk | ~8–12 GB |
| `fieldvault_data` volume (grows with use) | starts ~200 MB |

**Practical minimum:** plan for **15–20 GB free** on the filesystem that
holds this checkout before the first `docker compose up`. Laptops with
128 GB SSDs often need to reclaim space from large image scans, datasets,
or stale project trees on the same volume before the pull succeeds.

### Volume layout

Docker Compose writes relative to the parent of this checkout:

```
<parent>/
  fieldvault/           ← this repo
  fieldvault_data/      ← persistent store (created on first up)
  plugins/              ← plugin tree (optional bind mount)
```

---

## Manual init (dev)

Without Docker:

```bash
cd fieldvault
./scripts/init_store.sh
fieldvault init --scaffold
```

`init --scaffold` indexes the **parent** of `fieldvault/`, creates
`../fieldvault.db`, and writes `../plugins/`. The parent should be a
directory reserved for FieldVault when using this path.

`fieldvault init --dry-run` lists files that would be touched. It performs
no writes.

---

## Commands reference

| Command | Effect |
|---------|--------|
| `./scripts/deploy.sh` | Docker Compose up (pull + start) |
| `./scripts/init_store.sh` | Create store files in parent dir |
| `fieldvault init --scaffold` | Index parent + scaffold plugins |
| `fieldvault init --dry-run` | Print plan only |
| `./scripts/scaffold_plugin.sh <name>` | Add a plugin under `../plugins/` |
