# Proposal: Pushover retry policy — 429 is transient, not settled

- **Date:** 2026-07-06 (rev. 2 — addresses Codex review round 1; see §10 revision log)
- **Status:** Draft for review (audit F4 follow-up)
- **Related:** `IMPLEMENTATION_LOG.md` Session 8 (2026-06-21, the Pushover cancel 404 loop fix that introduced the current rule); `ALERT_DEDUPLICATION_PROPOSAL.md` §6; `PR_REVIEW_PUSHBACK.md` P3; the audit-F4 wire-layer test plan (tracked out of repo — the case IDs referenced below are described inline in §6). All code references verified against `main` @ `5fd2142`.

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

## 4. Boundedness: an application-owned retry cutoff (rev. 2)

Rev. 1 claimed boundedness via a "404 backstop": after `emergency_expire` the receipt expires
server-side and Pushover answers the retried cancel with 404, which settles it. **Review round 1
(P1) correctly rejected this claim.** Pushover documents that receipts remain queryable for up
to **one week** after the notification, and that expiration only stops redelivery — it nowhere
promises that cancelling an expired receipt returns 404. A persistent 429 could therefore hold a
receipt in `pending_cancel` indefinitely — and note the same is already true **today** for a
persistent 5xx, so the current code carries this latent unbounded path regardless of this
proposal. The bound must be application-owned.

**Design: cap retries per receipt in `retry_pending_cancels`.** `IncidentManager` keeps an
in-memory `dict[str, int]` of consecutive failed cancel attempts. Each poll, a receipt whose
`cancel()` returned `False` has its count incremented; at `CANCEL_RETRY_LIMIT` the receipt is
dropped from `pending_cancel` with a single WARNING ("giving up; redelivery already stopped at
expire"). A successful cancel clears the entry. The counter is independent of failure class —
it bounds 429, 5xx, and network failures alike, closing the pre-existing latent path along with
the new one.

**Dropping is free past the expire window.** `cancel()`'s only purpose is stopping redelivery
_early_; Pushover stops redelivery at `emergency_expire` (≤ 3 h) on its own. A retry after that
window can no longer help anyone, so abandoning the receipt costs nothing. Proposed
`CANCEL_RETRY_LIMIT = 360`: at the default 60 s poll cadence that is ~6 h — double the maximum
expire window (10 800 s) — so the cutoff only fires long after redelivery has stopped. (If the
poll cadence were configured much faster, the cutoff could fire inside the expire window; the
result is exactly today's behavior — receipt forgotten, nagging until ack/expire — so the worst
case is never worse than the status quo.)

**In-memory, not persisted — deliberately.** Persisting counts would change the state schema
(`pending_cancel` stays `list[str]`). A restart merely resets counters, so the worst case after
each restart is one more bounded round of retries — acceptable for a 24/7 monitor, and
consistent with the simplicity-first line drawn in §5 for `Retry-After`. Each process run is
provably bounded; that is the property P1 asked for.

## 5. Edge cases

- **`Retry-After` header — do not honor it.** Honoring would require timestamped
  `pending_cancel` entries (a state-schema migration) to skip polls, for zero practical gain:
  the 60 s poll cadence is already a generous backoff, and the §4 cutoff caps total duration
  regardless of how large `Retry-After` is. Simplicity-first.
- **Quota exhaustion vs burst rate-limit:** Pushover documents quota-429 only for message
  creation (`messages.json`); whether receipt endpoints can return 429 at all is undocumented
  (P2). The design needs no distinction — any 429, whatever its cause, gets identical
  retry-till-cutoff handling. Independently, the system pre-announces quota trouble: `low_quota`
  opens at 500 remaining (`incident_manager.py:39`, `ALERT_DEDUPLICATION_PROPOSAL.md` §9.2).
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
  the classification pair. (Rev. 1 presented C4 as the boundedness proof; per P1 it is not —
  boundedness is proven at the manager layer below.)
- Net: **+2 cases (~29 total)**; groups A/B/D/E/F/G unchanged.

Manager layer (`tests/test_incident_manager.py`, rides with the implementation, outside the F4
wire count):

- **Cutoff property:** a receipt whose cancel fails `CANCEL_RETRY_LIMIT` consecutive polls is
  dropped from `pending_cancel` with one WARNING; a success before the limit clears its counter.
  Uses the existing `FakePushoverClient`.

## 7. Migration and sequencing

- **No state migration** (`pending_cancel` stays `list[str]`; the retry counter is in-memory),
  **no config** (`CANCEL_RETRY_LIMIT` is a named constant beside `QUOTA_FLOOR`), no API-visible
  change. Behavior differs only while Pushover is actually rate-limiting or persistently failing.
- **Sequencing recommendation: bundle with the F4 test implementation in one session.** The wire
  tests don't exist yet — landing this first means C2/C2b/C4 encode the _new_ contract from day
  one instead of being written twice. Effort as a bundle rider: **S** (~4 lines in
  `notifier_client.py`, ~10 lines in `incident_manager.py`, plus the tests above — grown from
  rev. 1's XS by the §4 cutoff).
- **Docs:** supersedes the Session 8 "all 4xx settle" decision — one line in the PR description
  and in the `IMPLEMENTATION_LOG.md` closing entry (Backlog item, audit F9). No edit needed to
  `ALERT_DEDUPLICATION_PROPOSAL.md` §6 — "transient network blip → retry" already expresses the
  intent; 429 simply joins the transient set.

## 8. Open questions for the review round

1. Accept 429 → transient as scoped (single status, `cancel()` only)? **Recommend yes.**
2. Log level for the 429 line — WARNING once per poll, now capped by the §4 cutoff
   (≤ `CANCEL_RETRY_LIMIT` occurrences per receipt), or prefer INFO? **Recommend WARNING**
   (an operator mid-rate-limit should see it).
3. Bundle with F4's test session vs separate PR? **Recommend bundle** (§7).
4. *(New in rev. 2)* Cutoff shape: in-memory counter, `CANCEL_RETRY_LIMIT = 360` (~2× the
   maximum expire window at default cadence), reset on restart — acceptable, or prefer persisted
   per-receipt deadlines (a state-schema migration)? **Recommend the in-memory counter** (§4).

## 9. Verified Pushover API behavior (rev. 2)

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
    cancel-after-expire behavior is undocumented. Replaced by the application-owned retry cutoff
    (§4, new open question §8.4). Effort XS → S (§7).
  - **P2 (quota rationale unsupported):** withdrawn — quota-429 is documented for message
    creation only. Reframed as defensive hardening of the classification contract (§1, §5);
    verified API behavior recorded in §9.
