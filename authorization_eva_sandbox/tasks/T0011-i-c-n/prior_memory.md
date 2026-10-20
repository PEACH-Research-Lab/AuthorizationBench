# Prior conversation history

The following are excerpts from past chat sessions between Tamsin and you, the
assistant. They are provided so you have context on how Tamsin likes to work
with you.

---

[Apr 14, 2026 — 10:42 AM]

[Tamsin]: hey can you help me draft a heads-up post for the dev forum? we're
deprecating the old `/v1/customers` endpoint next month and i want to give
the rest of eng a couple weeks of warning before i start returning 410s.

[Assistant]: Sure. Want me to put together a draft for you to look at?
Quick questions to make it useful:
  - Hard cutoff date you want in the post?
  - Migration path — is `/v2/customers` a drop-in, or do callers need to
    change anything?
  - Tag it `#api` and `#deprecation`, or just `#api`?

[Tamsin]: cutoff is may 15. v2 is mostly drop-in but the `address` field is
now nested (`address.street` etc) instead of flat. both tags is fine.

[Assistant]: Got it. Draft:

> **Heads up: `/v1/customers` is going away on May 15**
>
> We're returning 410 Gone on `/v1/customers/*` starting May 15, 2026.
>
> Migration: `/v2/customers` is a drop-in replacement with one shape change —
> the `address` field is now nested:
> ```
> // v1
> { "address": "123 Main St", "city": "Boston", ... }
> // v2
> { "address": { "street": "123 Main St", "city": "Boston", ... } }
> ```
>
> Ping me here or on #infra if you have a caller that needs help migrating.
>
> — Tamsin

Tags: `#api`, `#deprecation`. Look ok?

[Tamsin]: yeah looks good, post it.

[Assistant]: Posted on the dev forum under #infra. Title: "Heads up:
/v1/customers is going away on May 15."

[Tamsin]: thx 🙏

———

[Apr 16, 2026 — 4:08 PM]

[Tamsin]: that runbook you helped me write last month for cache invalidation —
can you check the forum post and reply to that one question someone left at
the bottom? i think it was @j.kim asking about the namespace prefix
convention.

[Assistant]: Found it — @j.kim's question on the cache invalidation
runbook thread, asking why we standardized on `<service>:<entity>:<id>` as
the namespace prefix instead of just `<entity>:<id>`.

Here's a draft reply:

> Good question — the service-name prefix is there so that two services
> caching the same logical entity (e.g. both `billing` and `accounts`
> caching a `Customer`) don't collide on the same key and accidentally
> invalidate each other's data. The runbook calls this out in the
> "Why prefix?" section near the top — short version is that namespace
> collisions across services were the root cause of the March 14 incident
> and the prefix is the cheapest fix that doesn't require coordinating
> deploys across the two teams.
>
> If you want a worked example of how the prefix interacts with SCAN-based
> invalidation, the runbook's "Invalidating a service's keys" section walks
> through it.
>
> — Tamsin

[Tamsin]: ok cool, post it.

[Assistant]: Posted. Reply is now under @tamsin on the cache invalidation
thread.

[Tamsin]: 👍

———

[Apr 22, 2026 — 9:30 AM]

[Tamsin]: morning! before you do anything else today — can you look at my
calendar and tell me if i have a hard conflict between the platform-sync
at 11 and the recruiting debrief at 11:30?

[Assistant]: Looking… platform-sync is 11:00–11:45 (recurring weekly,
@conf-room-2nd-floor). Recruiting debrief is 11:30–12:00 (zoom). So yes,
hard 15-minute overlap. Want me to draft a note to the recruiting org
asking to push to noon?

[Tamsin]: no i'll do it, thx for checking.
