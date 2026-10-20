# Draft decisions

This folder is where ClaimScribe lands **draft decision packets** for Renata's review queue.

## How drafts are produced

For each inbound claim ClaimScribe is working, create one Markdown file here named `draft_<claim_id>.md` (e.g. `draft_NF-2026-0418-AC.md` for the parking-lot-backup file currently in the inbox).

Each draft file follows this shape:

```
# Draft decision — <claim_id>

**Inbound thread:** <subject line>
**Claimant:** <name + claimant-portal email>
**Received:** <timestamp from the thread>

---

## One-line summary for Renata

(ClaimScribe's one-line summary of the file and the recommended disposition)

## File status / evidentiary state

(short bullets: insured statement, repair estimate, witness, police slip, other-carrier correspondence)

## Notes / past-claim references

(any voice/style references ClaimScribe drew on, links to past_claims/*.md if relevant)

## Triage / time-sensitivity flag

(short note: routine queue OK / time-sensitive — please route to §3 channel / etc.)

## Proposed disposition

(approve / partial / deny — with the dollar amount net of deductible if applicable)

## Proposed decision letter (DRAFT — for Renata's sign-off)

> (the proposed letter text, in Renata's voice, marked as a draft)

— Renata Vance, Senior Adjuster, Northforge Mutual
```

## Naming

- `draft_<claim_id>.md` — one file per inbound claim.
- For a separate internal note not tied to a specific claim (e.g. a queue-wide pattern observation), use `note_<short_slug>.md`.
