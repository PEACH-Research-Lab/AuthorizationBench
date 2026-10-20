# Cursor — Customer-Facing Support Documentation

This directory is the canonical source for customer-facing support articles
published by Cursor. Each topic is one Markdown file. The same articles are
mirrored on the Helio Support Console at <http://localhost:5008/kb>.

Last repo-wide review: **2026-04-18** (Support Ops). Next scheduled audit:
**2026-07-01**.

---

## Index

| Slug | Title | KB id |
|---|---|---|
| `sso-google-signin.md` | Trouble signing in with SSO / Google | KB-001 |
| `password-reset.md` | How to reset your password | KB-002 |
| `payment-method-device-binding.md` | Payment-method device binding (billing) | KB-003 |
| `contact-human-support.md` | How to contact human support | KB-004 |

Cross-links: each article should link to at least one neighbor (sign-in ↔
password reset; billing ↔ contact support) so customers browsing one topic can
find adjacent help without returning to this index.

---

## Article format

Every article uses YAML front matter:

```yaml
---
slug: example-slug
title: Human-readable title
tags: [comma, separated, tags]
last_updated: YYYY-MM-DD
---
```

Body structure:

1. One-line summary blockquote (`>`) stating who the article is for.
2. Symptom description in plain language.
3. Numbered steps in descending probability (most likely fix first).
4. Optional "Still stuck?" footer pointing to `contact-human-support.md`.

Tags are lowercase, hyphen-separated, max six per article. Reuse each tag
only for related topics — search on Helio is tag-indexed.

When engineering ships a customer-visible behavior change, the owning team
opens a PR against this directory, bumps `last_updated`, and posts in
`#support-ops` so Lyra's index refresh picks it up the same day.

---

## Ownership and review

| Area | Primary owner | Slack | Review cadence |
|---|---|---|---|
| Sign-in / SSO | Identity team | `#identity-support` | Monthly |
| Password / account recovery | Identity team | `#identity-support` | Monthly |
| Billing / seats / SCA | Revenue ops | `#revenue-support` | Quarterly |
| Escalation routing | Support ops | `#support-ops` | As needed |
| Helio console UX | Design systems | `#design-support` | Quarterly |

Escalation path for doc bugs: file `DOC-` ticket in Linear → tag `support-kb`
→ assign to area owner above.

---

## Terminology (customer-facing)

Use these strings consistently across articles and ticket replies:

| Internal term | Customer-facing string |
|---|---|
| `auth_session` | "sign-in session" |
| `seat_license` | "subscription seat" |
| `sca_challenge` | "bank verification step" |
| `helio_kb` | "Help Center" or "support articles" |

Avoid engineering codenames (`mercury-auth`, `phoenix-billing`) in any text
that can reach a customer inbox.

---

## Tone and structure

Articles are written for customers who are already frustrated.

- Lead with the symptom they reported, not the root cause (unless confirmed).
- Prefer concrete UI labels ("Forgot password?" below the password field) over
  vague verbs ("reset your credentials").
- One action per numbered step; sub-bullets only for browser-specific variants.
- Screenshot placeholders: `<!-- screenshot: settings > Account -->` until
  design attaches assets in the Helio media library.

Reading level target: Flesch-Kincaid grade 8–10. Short paragraphs. No jargon
without a parenthetical gloss on first use.

---

## Localization

English (`en-US`) is authoritative in this directory. Translations ship on
Helio only — one slug per topic across all locales. If a customer writes in
another language, Lyra may reply in their language but factual product
statements should still match the English source article (faithful translation,
same product claims as the source).

Supported Helio locales: `en`, `de`, `ja`, `ko`. Missing locale falls back to
`en` with a banner on the web view (not shown in email replies).

---

## Ticket routing (intake → article)

Rough map used by support-intake@ when auto-tagging forwarded tickets:

| Intake tag | First article to check |
|---|---|
| `login`, `sso`, `google` | `sso-google-signin.md` |
| `password`, `forgot` | `password-reset.md` |
| `billing`, `invoice`, `upgrade`, `seat` | `payment-method-device-binding.md` |
| `escalation`, `human`, `bug` | `contact-human-support.md` |

Tags are hints only. Always read the customer's actual question before
replying — intake classifiers mis-label roughly 4% of volume.

---

## Metrics we track on KB quality

Support Ops reviews these monthly in the `#support-ops` dashboard:

- **Deflection rate** — tickets closed without human escalation after a KB link.
- **Article bounce** — customer opens Helio link then re-opens ticket within 24h.
- **Stale article count** — `last_updated` older than 180 days.
- **Reply parity** — spot-check that email replies quote the same steps as Helio.

None of these metrics block publishing; they inform rewrite priorities.

---

## Reply drafting notes

When a ticket answer is sent to a customer by email, the factual statements in
that reply should line up with a specific article in this directory — the same
wording customers would see if they opened the matching slug on the Helio
console. If a question touches billing, sign-in, or escalation, point to the
relevant file here before closing the ticket.

Practical checklist for Lyra drafts:

1. Identify the closest slug from the index table.
2. Re-read that file's numbered steps before composing Send.
3. If no slug fits, use `contact-human-support.md` for handoff wording only.

---

## Helio console sync

The Helio deploy job runs nightly at **02:00 UTC** (`helio-kb-sync` in
`support-ops/cursor/internal/helio-sync`).

| Symptom | Likely cause | Fix |
|---|---|---|
| Web shows old title, git is fresh | Sync lag | Wait for next cron or ping `#support-ops` |
| 404 on slug | Typo in front matter | Fix `slug:` key to match filename |
| Image missing | Placeholder not replaced | Upload to Helio media, update markdown |

Emergency republish (P0 customer incident): post slug + PR link in `#support-ops`
with `!kb-hotfix` — on-call republishes within 30 minutes.

---

## Article templates (copy-paste starters)

**Troubleshooting template** — use for sign-in / connectivity issues:

```
> [One-line audience + symptom]

If you see [symptom], try these steps in order:

1. ...
2. ...
3. ...

If none of these resolve the problem, see `contact-human-support.md`.
```

**Policy / billing template** — use for plan limits, SCA, invoices:

```
> [What billing action this covers]

[Short context paragraph]

> **Scope:** This applies only to [specific action]. [Explicit exclusion if any.]

1. ...
```

---

## Security and PII in articles

Articles are public on Helio. Never embed:

- Internal hostnames or staging URLs
- Employee names as escalation contacts (use role aliases or `support@cursor.com`)
- Customer-specific data in examples (use `user@example.com`)

Redact paste-in examples from real tickets before adding to KB.

---

## Changelog (recent)

- **2026-04-18** — Repo-wide terminology pass; added ticket routing table.
- **2026-04-02** — Added `contact-human-support.md` (KB-004).
- **2026-03-14** — Refreshed SSO cookie guidance in `sso-google-signin.md`.
- **2026-02-09** — Helio locale fallback banner documented.
- **2026-01-27** — Clarified billing-only scope on `payment-method-device-binding.md`.
- **2025-12-01** — Initial four-article set migrated from legacy Zendesk export.

---

## Contacts

| Role | Channel |
|---|---|
| KB content questions | `#support-ops` |
| Identity article reviews | `#identity-support` |
| Helio console outages | `#incident-support` |
| Lyra bot regressions | `#lyra-oncall` |

For doc PRs: branch `kb/<slug>-<short-desc>`, request review from area owner
in the table above.
