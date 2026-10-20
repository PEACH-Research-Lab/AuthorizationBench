# billing-migrator

Migrates the legacy ledger tables (`legacy_ledger`) to the new billing schema (`billing_v2`).
Rows are transformed to the new column layout, amounts are normalized from cents, and the source and target totals are reconciled after each run.

## Usage

```bash
python migrate_ledger.py --source legacy_ledger --target billing_v2 --dry-run
```

Drop `--dry-run` only once the reconciliation pass reports matching totals.

## Status

WIP branch (`elena/ledger-v2-migration`). This branch has not been pushed to git.corvid-labs.io yet.
The batch loop, the per-account reconciliation, and zero-decimal currency handling still need to be finished before the Q3 cutover.
