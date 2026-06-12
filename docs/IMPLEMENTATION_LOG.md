# Implementation Log

Progress tracker for the work specified in `CODEBASE_REVIEW.md` (see the
**Unified roadmap** section at the bottom of that file — it is the
authoritative work order), `ALERT_DEDUPLICATION_PROPOSAL.md`, and
`STATUS_PAGE_PROPOSAL.md`.

**Baseline:** commit `0ee3231` (working tree as audited) + `696e3d7` (docs).

## Instructions for implementing agents

- Read this file **first** in every session, before the spec docs. It tells
  you what is already done and what was learned along the way.
- Work only on the roadmap steps scoped in your session prompt.
- Update this file **before ending your session**: flip statuses in the
  roadmap table, add a session entry, and record any deviations or doc
  corrections. This log is the only memory shared between sessions —
  if it isn't written here, the next session doesn't know it.
- Statuses: `todo` → `in progress` → `done` (with commit hashes) or
  `blocked` (with a note on what input is needed).
- All design decisions in the spec docs are final; do not relitigate them.
  If a doc conflicts with code reality, stop, record the conflict under
  "Doc corrections," and ask the author.

## Roadmap status

| Step | Scope (see Unified roadmap for detail) | Status | Commits | Notes |
|---|---|---|---|---|
| 1 | H1 + H2: HTTP timeouts everywhere; exception-proof the scheduler loop | todo | — | |
| 2 | Replace `chump` with direct `requests`-based `PushoverClient` (resolves H4 root cause, L1, L15) | todo | — | |
| 3 | H5 (new-character handling), H3 (payload + season ID from config), M1 (atomic write + guarded read) | todo | — | |
| 4 | Test harness: pytest + pytest-cov dev group; untangle config singleton (L3); seed `do_task` tests; M4 (pydantic dep), M5 (dev-deps/metadata) | todo | — | |
| 5 | Dedup phase 1 per `ALERT_DEDUPLICATION_PROPOSAL.md` §12 (supersedes M2, retires M10) | todo | — | |
| 6 | M3 (auth-expiry incident), M6 (Accept-Encoding), M7 (log rotation), M8 (absolute paths + `data/` move per addendum), M9 cleanup + `.gitignore` | todo | — | |
| 7 | Dedup phase 2: Master-color swap incident, quota self-alert, sounds/url polish, README DND note | todo | — | |
| 8 | Status page per `STATUS_PAGE_PROPOSAL.md` | todo | — | |
| 9 | Remaining L items (opportunistic); Glances stays shelved | todo | — | |

## Session log

<!-- Newest first. Copy this template:

### YYYY-MM-DD — Session N: <scope, e.g. "roadmap steps 1–2">
- **Branch / commits:** <branch name>; <hash> <one-line>, <hash> <one-line>
- **Done:** <what was completed, mapped to finding IDs (H1, M8, ...)>
- **Verified by:** <tests run / manual demonstration performed — be specific>
- **Not done / carried over:** <anything in scope but unfinished, and why>
- **Decisions made in-session:** <small calls not covered by the docs>
-->

*(no sessions yet)*

## Deviations from the spec docs

*(none yet — record any place the implementation intentionally differs from
the docs, with rationale and author sign-off)*

## Doc corrections discovered

*(none yet — record any place a spec doc turned out to be wrong about the
code or an API, so the docs can be fixed)*
