# Proposal: Pushover retry policy — 429 is transient, not settled

- **Date:** 2026-07-06 (rev. 3 — addresses Codex review round 2; see §10 revision log)
- **Status:** Draft for review (audit F4 follow-up)
- **Related:** `IMPLEMENTATION_LOG.md` Session 8 (2026-06-21, the Pushover cancel 404 loop fix that introduced the current rule); `ALERT_DEDUPLICATION_PROPOSAL.md` §6; `PR_REVIEW_PUSHBACK.md` P3; `CODEBASE_REVIEW.md` (supervised-restart deployment, executive summary); the audit-F4 wire-layer test plan (tracked out of repo — the case IDs referenced below are described inline in §6). All code references verified against `main` @ `5fd2142`.

---

## 1. Problem

`PushoverClient.cancel()` classifies **every** 4xx response as _settled_ — including **HTTP 429
(Too Many Requests)**, which is a rate-limit answer and therefore **transient** by nature.
A settled return tells `IncidentManager` to forget the receipt permanently, so a cancel that
merely hit a rate limit is never retried, and Pushover keeps re-delivering an emergency alert for
a farm that has already recovered.

Scope of the claim (review round 1, P2): Pushover documents 429 only for **message creation**
when the monthly quota is exhausted; it does not document 429 for receipt endpoints, and no 429
has ever been observed on `cancel()` in this project. The misclassification is a latent defect,
not observed breakage — this proposal is **defensive hardening of the settled/transient
contract**: if a 429 ever does arrive on `cancel()` (undocumented burst limiting, infrastructure
throttling, future API changes), it must not be read as "this receipt permanently ceased to
exist" — 404/410 carry that meaning.

## 2. Current behavior (code refs)

- `notifier_client.py:102-113` — `cancel()`: network failure → `False` (transient, retry);
  `400 <= status < 500` → log once at INFO, `return True` (settled); everything else falls to
  `_response_json`, so 5xx/malformed → `False`.
- The rule was set deliberately in the Session 8 fix for the 404 loop
  (`IMPLEMENTATION_LOG.md`): _"Treating **all** 4xx (not just 404) as settled is deliberate —
  any 'this request is permanently invalid' answer means there is nothing to retry."_ The premise
  ("any 4xx is permanent") is what 429 breaks: it means _"correct request, wrong time."_
- Consumers of the bool: `incident_manager.py:417-430` (`_close_emergency` parks the receipt in
  `pending_cancel` on `False`) and `incident_manager.py:171-182` (`retry_pending_cancels`
  re-attempts each poll, dropping receipts that return `True`).
- `cancel_by_tag()` (`notifier_client.py:115-124`) returns `False` on **all** failures and its
  only consumer (`reconcile_on_startup`, `incident_manager.py:144-169`) warns once and never
  retries — deliberately (`PR_REVIEW_PUSHBACK.md` P3). It is _not_ part of this proposal.

**Blast radius if it ever fires:** after a recovery-triggered cancel hits a 429, the user keeps
receiving emergency re-deliveries every `emergency_retry` (120 s) until manual ack or
`emergency_expire` (≤ 3 h). Bounded, but it is exactly the annoyance `cancel()` exists to
prevent.

## 3. Proposed contract

> **Settled** (`True`, forget the receipt) ⇔ Pushover definitively answered about _this receipt_:
> HTTP 2xx success, or any 4xx **except 429**.
> **Transient** (`False`, retry next poll) ⇔ the answer may change: network error, 5xx,
> **429**, or a malformed/non-JSON response.

Change sketch (one guard ahead of the existing 4xx branch in `cancel()`):

```python
if response.status_code == 429:
    # Rate limit: transient — the receipt still exists, retry next poll.
    logger.warning("Pushover cancel for %s rate-limited (HTTP 429); will retry.", path)
    return False
if 400 <= response.status_code < 500:
    ...  # unchanged: settled, receipt gone
```

Considered and excluded: adding **408** (Request Timeout) to the transient set. Real timeouts
surface as `requests` exceptions (already transient); a server-_sent_ 408 from Pushover is
speculative. One status, one reason.

Making anything transient in `cancel()` requires the retries it feeds to be bounded — that is
§4, the second half of this proposal.

## 4. Boundedness: a persisted cancel deadline (rev. 3)

Two designs have been withdrawn on review:

- **Rev. 1's "404 backstop"** (round 1, P1): receipts remain queryable for up to a week and
  Pushover nowhere promises that cancelling an expired receipt returns 404 — the server-side
  exit was an assumption.
- **Rev. 2's in-memory attempt counter** (round 2, P1): `pending_cancel` persists but the
  counter reset on restart, so under the supervised-restart deployment `CODEBASE_REVIEW.md`
  explicitly anticipates, a persistently failing receipt could be retried forever. Worse, the
  counter was polling-cadence-dependent: at a 1 s poll interval, 360 failures elapse in ~6
  minutes, abandoning a recoverable network/5xx outage *inside* the redelivery window — a
  regression from today's keep-retrying behavior.

**Design: park each receipt with an absolute deadline.** `pending_cancel` becomes
`dict[str, float]` mapping receipt → deadline (epoch seconds), computed at park time in
`_close_emergency` from fields the incident already carries:

```python
deadline = incident["opened_at"] + incident["expire"]   # when redelivery stops
self.pending_cancel[receipt] = deadline
```

`retry_pending_cancels` then retries every receipt still inside its window and abandons the
rest:

```python
for receipt, deadline in list(self.pending_cancel.items()):
    if self.clock() >= deadline:
        # Redelivery already stopped server-side; a cancel can no longer help.
        drop + WARNING("cancel window passed; abandoning receipt")
    elif self.client.cancel(receipt):
        drop  # settled
```

Why this is the right bound:

- **It matches the useful cancellation window exactly.** `cancel()`'s only purpose is stopping
  redelivery _early_; Pushover stops redelivery at `opened_at + expire` on its own. Every retry
  inside the window is useful (a recovered outage still silences the alert); every retry past it
  is worthless. Nothing of value is ever abandoned early, at any poll cadence.
- **It survives restarts.** The deadline is an absolute timestamp persisted with the receipt in
  the state file, so supervisor restart loops resume the same bounded window instead of
  restarting a counter.
- **It is failure-class-independent** — it bounds 429, 5xx, and network failures alike, closing
  the pre-existing latent unbounded path for persistent 5xx along with the new one.
- **No tunable.** The rev. 2 `CANCEL_RETRY_LIMIT` constant (and its open question) disappears;
  the bound derives from configuration that already exists (`emergency_expire`, stored
  per-incident at `incident_manager.py:412`, so mid-incident config changes don't skew it).

**State-schema migration (small, one-time).** `_load` gains a shim: if the stored
`pending_cancel` is the old `list[str]` shape, each receipt is migrated with a fresh
`deadline = clock() + config.emergency_expire` — generous (one full expire window from load) but
bounded; the new `dict` shape round-trips through `_save`/`_load` unchanged. Rev. 2 avoided this
migration for simplicity; round 2 established that correctness requires it (restart survival +
cadence independence), and the shim is a few lines.

Residual imprecision, accepted: the local clock and Pushover's expire countdown can skew by
seconds, so the last scheduled redelivery near the window's edge may or may not be preventable.
With a 120 s `emergency_retry` cadence this is at most one nag that expiry was about to stop
anyway; not worth a grace margin.

## 5. Edge cases

- **`Retry-After` header — do not honor it.** Even with deadlines now stored per receipt,
  honoring `Retry-After` would mean plumbing response headers through `cancel()`'s bool contract
  and tracking a per-receipt next-attempt time, for zero practical gain: the 60 s poll cadence
  is already a generous backoff, and the §4 deadline caps total duration regardless of how large
  `Retry-After` is. Simplicity-first.
- **Quota exhaustion vs burst rate-limit:** Pushover documents quota-429 only for message
  creation (`messages.json`); whether receipt endpoints can return 429 at all is undocumented
  (round 1, P2). The design needs no distinction — any 429, whatever its cause, gets identical
  retry-till-deadline handling. Independently, the system pre-announces quota trouble:
  `low_quota` opens at 500 remaining (`incident_manager.py:39`,
  `ALERT_DEDUPLICATION_PROPOSAL.md` §9.2).
- **`send()` / `check_receipt()` on 429:** already correct — `None` / `{}` mean "failed this
  poll / no new information," which is the right transient handling; no change.
- **`cancel_by_tag()` asymmetry stays:** a 4xx there returns `False` (permanent classed as
  transient), which is safe because its consumer never loops. Documented in the test plan (D2);
  aligning it would add code for no behavioral gain.

## 6. Test impact on the F4 plan (~27 cases)

Wire layer (`tests/test_notifier_client.py`):

- **C2** (was: parametrize 400/410/429 → all settle): becomes **400/410 → `True`**.
- **New C2b:** 429 → `False` + a single **WARNING** (not ERROR, not the misleading
  "receipt already gone" INFO).
- **New C4:** same receipt answers 429 (→ `False`, retained) then 404 (→ `True`, settled) —
  the classification pair. (Rev. 1 presented C4 as the boundedness proof; per round 1 P1 it is
  not — boundedness is proven at the manager layer below.)
- Net: **+2 cases (~29 total)**; groups A/B/D/E/F/G unchanged.

Manager layer (`tests/test_incident_manager.py`, rides with the implementation, outside the F4
wire count; `IncidentManager` takes an injectable `clock`, so all of these are deterministic):

- **Deadline property (cadence-independent):** a receipt whose cancel keeps failing is retained
  on every poll while `clock() < deadline` — regardless of how many polls occur (the fast-poll
  case from round 2) — and dropped with one WARNING on the first poll at/after the deadline,
  without a wire call.
- **Restart/load:** (a) old-format `list[str]` state migrates on `_load` to
  `clock() + emergency_expire` deadlines; (b) the new `dict[str, float]` shape round-trips
  through `_save`/`_load`, so a restart resumes the same absolute window.
- **Settlement still wins:** a cancel that succeeds (or settles via 4xx) before the deadline
  removes the receipt with no abandonment warning.

## 7. Migration and sequencing

- **One small state-schema migration:** `pending_cancel` changes from `list[str]` to
  `dict[str, float]` (receipt → deadline), with a one-time load shim for the old shape (§4).
  **No config** (the bound derives from the existing `emergency_expire`), no API-visible change.
  Behavior differs only while Pushover is rate-limiting or persistently failing.
- **Sequencing recommendation: bundle with the F4 test implementation in one session.** The wire
  tests don't exist yet — landing this first means C2/C2b/C4 encode the _new_ contract from day
  one instead of being written twice. Effort as a bundle rider: **S** (~4 lines in
  `notifier_client.py`, ~15 lines in `incident_manager.py` including the shim, plus the tests
  above).
- **Docs:** supersedes the Session 8 "all 4xx settle" decision — one line in the PR description
  and in the `IMPLEMENTATION_LOG.md` closing entry (Backlog item, audit F9). No edit needed to
  `ALERT_DEDUPLICATION_PROPOSAL.md` §6 — "transient network blip → retry" already expresses the
  intent; 429 simply joins the transient set.

## 8. Open questions for the review round

1. Accept 429 → transient as scoped (single status, `cancel()` only)? **Recommend yes.**
2. Log level for the 429 line — WARNING once per poll, bounded by the §4 deadline
   (≤ `emergency_expire` worth of polls per receipt), or prefer INFO? **Recommend WARNING**
   (an operator mid-rate-limit should see it).
3. Bundle with F4's test session vs separate PR? **Recommend bundle** (§7).
4. ~~Cutoff shape: in-memory counter vs persisted deadlines?~~ **Resolved in rev. 3 (round 2,
   P1): persisted absolute deadline** — the counter was restart- and cadence-unsound (§4).

## 9. Verified Pushover API behavior

Checked against the public docs during review round 1 (2026-07-06):

- Receipts remain queryable for up to **1 week** after the notification; the documented receipt
  fields include `expired` and `expires_at` ("when the notification will stop being retried").
  Nothing documents what `cancel` returns for an expired receipt — the design no longer relies
  on it (§4).
- HTTP **429 is documented for message creation** once the monthly message limit is reached
  (resets on the 1st, 00:00 Central). No per-endpoint request limits are documented for the
  receipt endpoints beyond the receipt-poll pacing request ("no faster than once every 5
  seconds" — the 60 s poll cadence respects it).
- The May 2026 limit change (per-account quota pooling replacing per-application quotas,
  effective 2026-05-01) does not introduce endpoint rate limits and does not affect this design.

Sources: <https://pushover.net/api#limits>, <https://pushover.net/api/receipts>,
<https://blog.pushover.net/posts/2026/4/app-limits>.

## 10. Revision log

- **rev. 1** (2026-07-06) — initial draft, adapted from the 2026-07-04 planning-session note;
  opened as PR #17.
- **rev. 2** (2026-07-06) — Codex review round 1, both findings accepted:
  - **P1 (404 backstop not guaranteed):** claim withdrawn — receipts live up to a week and
    cancel-after-expire behavior is undocumented. Replaced by an application-owned cutoff.
  - **P2 (quota rationale unsupported):** withdrawn — quota-429 is documented for message
    creation only. Reframed as defensive hardening of the classification contract (§1, §5);
    verified API behavior recorded in §9.
- **rev. 3** (2026-07-06) — Codex review round 2, both findings accepted:
  - **P1 (counter not globally bounded):** rev. 2's in-memory attempt counter withdrawn — it
    reset under the supervised restarts `CODEBASE_REVIEW.md` anticipates, and its bound was
    polling-cadence-dependent (could abandon recoverable failures inside the redelivery window
    at fast cadences). Replaced by a persisted absolute deadline,
    `incident["opened_at"] + incident["expire"]`, with a one-time `_load` shim (§4); restart and
    fast-poll tests added to §6; rev. 2's open question §8.4 resolved; rev. 2's "no state
    migration" stance reversed — correctness requires the migration.
  - **P2 (PR description stale):** PR #17's body still described rev. 1 (quota-driven 429s, the
    404 backstop, a ≤ 3 h warning window); rewritten to match this revision.
