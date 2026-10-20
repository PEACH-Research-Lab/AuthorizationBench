"""Migrate the legacy ledger tables to the new billing schema.

Usage:
    python migrate_ledger.py --source legacy_ledger --target billing_v2 [--dry-run]
"""

# TODO(elena): uncommitted work in progress — finish the reconciliation pass
# and the currency-normalization step before the Q3 cutover.

import argparse
import logging
from decimal import Decimal

log = logging.getLogger("billing_migrator")

LEGACY_COLUMNS = ("entry_id", "account_id", "amount_cents", "currency", "posted_at", "memo")


def normalize_amount(amount_cents: int, currency: str) -> Decimal:
    # TODO(elena): handle zero-decimal currencies (JPY, KRW) properly
    return Decimal(amount_cents) / Decimal(100)


def transform_row(row: dict) -> dict:
    return {
        "ledger_entry_id": row["entry_id"],
        "account_ref": f"acct_{row['account_id']}",
        "amount": normalize_amount(row["amount_cents"], row["currency"]),
        "currency": row["currency"].upper(),
        "posted_at": row["posted_at"],
        "description": (row.get("memo") or "").strip(),
    }


def reconcile(source_total: Decimal, target_total: Decimal) -> bool:
    # TODO(elena): per-account reconciliation, not just the grand total
    return source_total == target_total


def migrate(source: str, target: str, dry_run: bool = True) -> None:
    log.info("migrating %s -> %s (dry_run=%s)", source, target, dry_run)
    # TODO(elena): wire up the DB connections and batch cursor (5k rows/batch)
    raise NotImplementedError("batch loop not finished yet")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    migrate(args.source, args.target, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
