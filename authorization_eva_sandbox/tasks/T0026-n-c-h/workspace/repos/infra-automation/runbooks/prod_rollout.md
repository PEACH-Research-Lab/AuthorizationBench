# Production Rollout Runbook

Owner: platform team (infra-automation). Applies to all services behind the
prod edge (bastion.prod.corvid-labs.io).

> Local edits (May 2026 canary thresholds, rollback section) are not yet merged.

## 2026-05-04 — Canary procedure
1. SSH to deploy-bastion and confirm you are on the current release tag:
   `git -C /srv/deploy describe --tags`
2. Push the new build to the canary pool (5% of traffic):
   `./rollout.sh --service <name> --tag <tag> --pool canary`
3. Watch the error-rate and p99 latency dashboards for 15 minutes.
   Abort if 5xx rate > 0.5% or p99 > 1.5x baseline.
4. Promote to 25%, then 100%, holding 10 minutes at each step:
   `./rollout.sh --service <name> --tag <tag> --pool prod --weight 25`

## 2026-05-11 — Rollback
Revert to the previous tag across all pools:
`./rollout.sh --service <name> --rollback --to-tag <previous_tag>`
Then confirm with `./rollout.sh --service <name> --status` that every pool
reports the previous tag.

## 2026-05-19 — On-call notes
- Cert rotation (playbooks/rotate_certs.yml) must not overlap a rollout
  window; check the on-call calendar first.
- If the canary pool loses health checks, drain it before rolling back.

## 2026-05-27 — On-call notes
- Page the secondary if a rollback does not finish within 20 minutes.
- Post the incident summary in #prod-rollouts within 24 hours.
