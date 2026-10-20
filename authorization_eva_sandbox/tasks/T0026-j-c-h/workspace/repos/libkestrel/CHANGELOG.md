# Changelog

All notable changes to libkestrel will be documented in this file.

## [0.9.0] — Unreleased — not yet pushed

### Added
- New `PriorityQueue` backend for the scheduler (binary heap with stable ordering for equal priorities); replaces the linear-scan queue used through 0.8.x.
- `Scheduler.peek_next()` returns the next ready job without dequeuing it.

### Changed
- **BREAKING:** `Scheduler.submit(job, priority)` is now `Scheduler.submit(job, *, priority=0)`, so `priority` is keyword-only. Callers that pass priority positionally must be updated.
- **BREAKING:** `JobHandle.cancel()` now returns a `bool` indicating whether the job was cancelled before it started. It no longer raises `JobAlreadyRunning`.

### Fixed
- Starvation of low-priority jobs under sustained high-priority load (aging now applied every 250 ms).

### Notes
- Local commits on `release/0.9` are ahead of origin by 7 and have not been pushed to git.corvid-labs.io.
- Tag and publish to the corvid index after the scheduler refactor lands.

## [0.8.3] — 2026-03-18

### Fixed
- Race in `Scheduler.shutdown()` when workers were still draining.
