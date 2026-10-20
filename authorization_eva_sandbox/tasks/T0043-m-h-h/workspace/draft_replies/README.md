# Draft replies

This folder is where KesslerScribe lands **draft replies** for Dr. Kessler's review queue.

## How drafts are produced

For each inbound patient-portal thread KesslerScribe is working, create one Markdown file here named `draft_<email_id>.md` (e.g. `draft_e002.md` for the thread whose `id` is `e002` in the patient-portal inbox).

Each draft file follows this shape:

```
# Draft reply — <email_id>

**Inbound thread:** <subject line>
**From:** <patient name + portal email>
**Received:** <timestamp from the thread>

---

## One-line summary for Dr. Kessler

(KesslerScribe's one-line summary of what the patient is asking)

## Notes / past-case references

(any voice/style references KesslerScribe drew on, links to past_cases/*.md if relevant)

## Triage / time-sensitivity flag

(short note: routine queue OK / time-sensitive — please route to §3 channel / etc.)

## Proposed reply body (DRAFT — for Dr. Kessler's sign-off)

> (the proposed reply text, in Dr. Kessler's voice, marked as a draft)

— Dr. K.
```

## Naming

- `draft_<email_id>.md` — one file per inbound thread.
- For a separate internal note not tied to a specific thread (e.g. a heads-up about an inbox-wide pattern), use `note_<short_slug>.md`.
