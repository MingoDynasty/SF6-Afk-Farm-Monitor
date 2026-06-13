# PR Review Response — incident manager (PRs #3 / #4)

- **Date:** 2026-06-13
- **Reviews covered:** the `IncidentManager` findings raised against
  [#3](https://github.com/MingoDynasty/SF6-Afk-Farm-Monitor/pull/3) (dedup
  phase 1) and inherited by
  [#4](https://github.com/MingoDynasty/SF6-Afk-Farm-Monitor/pull/4) (step 6).
- **Where the fixes live:** `dedup-phase-1` (the branch that authored the code),
  so both PRs inherit them.

This document records which review findings were accepted, which suggested
remedy was **declined**, and the reasoning — so the decision isn't lost at merge
time.

| Finding | Verdict | Action |
|---|---|---|
| **P2** — open incident with `receipt=None` stays silent forever after a disabled→enabled restart | **Accepted** | Fixed: send the first real alert on re-enable. New reload test. |
| **P3** — `cancel_by_tag()` failure during startup reconciliation is silently ignored | **Observation accepted; suggested remedy (persist failed tags for retry) declined** | Log the failure (the real defect was invisibility). Did **not** add tag-retry — it is unsafe here. |

---

## P2 — accepted and fixed

**Finding:** if an incident is recorded while `pushover_enabled = false`, it is
persisted with `receipt = None`. After a restart with Pushover enabled while the
condition is still active, `_maintain_*` treats `receipt is None` as
"permanently silent," so the first *real* emergency alert is never sent.

**Why it matters:** a missed alert is the one failure this app exists to
prevent. The trigger is plausible — test with Pushover off, then flip it on for
production while a stuck/auth condition is ongoing across the restart. (The M8
verification run did exactly the disabled-then-persist half of this.)

**Fix:** split the silent guard in the emergency maintain path. `not enabled`
stays silent; `enabled and receipt is None` now sends the first real emergency
alert and attaches a receipt (reusing the existing re-raise path). This is
unambiguous because an *enabled* open with a failed send is never recorded — so
`receipt is None` on a persisted incident can only mean "opened while disabled."

Chosen over the reviewer's alternative ("don't persist alertable incidents while
disabled") because the proposal (§7) deliberately keeps incidents
opening/closing while disabled for log consistency; send-on-enable preserves
that behavior and still closes the gap.

**Coverage:** `test_incident_opened_while_disabled_alerts_after_reenable` —
disabled open (receipt `None`, persisted) → reload enabled → next evaluate sends
exactly one priority-2 alert and persists a real receipt.

---

## P3 — observation accepted; suggested remedy declined

**Finding:** `reconcile_on_startup()` ignores the boolean returned by
`cancel_by_tag()` and immediately saves clean rebuilt state. If the state file
is missing/corrupt *and* the tag-cancel hits a transient failure, dangling
server-side emergency retries can continue until ack/expiry while the app opens
a fresh incident.

**What we accepted:** the genuine defect is that the failure was **invisible**.
Fixed by logging a warning when `cancel_by_tag()` returns `False`.

**What we declined — "persist failed tags for retry":** this is unsafe as
stated, because **Pushover tags here are per-incident-*kind* and reused across
occurrences** (`sf6mon-stuck_farm` is a constant). Sequence:

1. Reconcile fails to cancel tag `sf6mon-stuck_farm`; the tag is parked for
   retry.
2. A later poll opens a **new** stuck-farm incident and sends an emergency with
   the **same** tag, receiving receipt `R_new`; Pushover begins re-delivering
   `R_new`.
3. The parked retry fires `cancel_by_tag(sf6mon-stuck_farm)` → it cancels
   `R_new`'s server-side retries.

The result is a live emergency silently downgraded from "nags until ack" to a
single delivery — i.e. the exact missed-wake-up the app exists to prevent. So
the naive retry is a worse failure than the one it fixes. (This is *unlike* the
existing `pending_cancel` list, which retries by **receipt** — a unique,
occurrence-specific handle that cannot clobber a later incident.)

**Why log-and-accept is proportionate:**

- **Bounded & self-healing.** The stale nag stops on ack or at `expire`
  (≤ 3 h). Pushover keeps re-delivering, so the user will ack it.
- **Triple-compound trigger.** It needs a missing/corrupt state file **and** a
  transient cancel failure at that instant **and** pre-existing dangling
  server-side retries. (First-ever run takes the missing-state path too, but
  there is nothing on the server to cancel, so a failure there is harmless.)
- **Already within the documented envelope.** `ALERT_DEDUPLICATION_PROPOSAL.md`
  §6 states reconciliation's accepted worst case is "one duplicate alert," and
  frames cancel failures as "annoying, not harmful."

**If full robustness were ever required** (it isn't, for a single personal
deployment): a *safe* retry must invalidate a parked tag the moment a new
same-kind incident opens (drop the tag from the pending set in the open path,
before sending). That is real added machinery and is deliberately deferred.

**Coverage:** `test_reconcile_logs_when_cancel_by_tag_fails` — a failing
`cancel_by_tag` is logged at WARNING and reconciliation still completes (clean
state persisted).
