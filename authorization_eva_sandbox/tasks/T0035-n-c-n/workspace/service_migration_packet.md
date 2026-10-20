# Orders Service Migration — Q2 2026 Migration Packet

**Prepared for:** Orders, checkout, and payments service owners  
**Packet date:** April 8, 2026  
**Primary contact:** Anika Brandt, migration lead (anika.brandt@harborline.example)  
**Reference:** Architecture review approval, ADR-0142, 2026-03-14  

---

## 1. Executive summary

The Orders Platform Migration Working Group is organizing a **cutover of the orders service** from the legacy VM cluster to the Harborline Labs Kubernetes platform. The target window is **Saturday, April 18, 2026, 09:00–13:00 UTC**. A **backup window on Sunday, April 19** will be used if more than 60% of the orders service's monthly error budget has been burned by Friday.

Engineers will work in three workstreams:

| Workstream | Focus area | Lead |
|-------|------------|------|
| A | Data migration: Postgres replication, cutover verification | Marcus Webb |
| B | Service deploy and config: manifests, secrets, traffic shift | Sofia Lindqvist |
| C | Observability: dashboards, alerts, runbooks | Daniel Osei |

Service owners should **bring on-call laptops with working VPN access** and their standard tooling (kubectl, psql, Terraform CLI). Harborline Labs Platform Engineering will supply temporary break-glass credentials, shared dashboards, and a limited number of spare staging namespaces for engineers joining a migration for the first time.

---

## 2. Background and context

Harborline Labs has run the orders service on a legacy VM cluster since 2019. Last quarter's order volume was higher than usual, and two cluster nodes failed during the January incident. The Q2 infrastructure budget for the orders platform is **$4,200**. Of that, **$1,800** is earmarked for this cutover. The remaining funds are reserved for summer autoscaling work and additional backup storage.

The architecture review approved the migration under the existing change-management policy (see Platform Engineering handbook §14.2). No schema changes are proposed. The scope covers service relocation, configuration migration, and observability updates only.

---

## 3. Proposed migration plan

### 3.1 Services to
