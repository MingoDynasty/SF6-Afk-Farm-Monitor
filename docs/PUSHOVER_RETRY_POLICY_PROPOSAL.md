# Proposal: Pushover retry policy — 429 is transient, not settled

- **Date:** 2026-07-06
- **Status:** Draft for review (audit F4 follow-up)
- **Related:** `IMPLEMENTATION_LOG.md` Session 8 (2026-06-21, the Pushover cancel 404 loop fix that introduced the current rule); `ALERT_DEDUPLICATION_PROPOSAL.md` §6; `PR_REVIEW_PUSHBACK.md` P3; the audit-F4 wire-layer test plan (tracked out of repo — the case IDs referenced below are described inline in §6). All code references verified against `main` @ `5fd2142`.

---

## 1. Problem

`PushoverClient.cancel()` classifies **every** 4xx response as _settled_ — including **HTTP 429
(Too Many Requests)**, which is a rate-limit/quota answer and therefore **transient** by nature.
A settled return tells `IncidentManager` to forget the receipt permanently, so a cancel that
merely hit a rate limit is never retried, and Pushover keeps re-delivering an emergency alert for
a farm that has already recovered.

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

**Blast radius today:** after recovery-triggered cancel hits a 429, the user keeps receiving
emergency re-deliveries every `emergency_retry` (120 s) until manual ack or `emergency_expire`
(≤ 3 h). Bounded, but it is exactly the annoyance `cancel()` exists to prevent — and it is most
likely precisely during quota exhaustion, i.e. when things are already going wrong.

## 3. Proposed contract

> **Settled** (`True`, forget the receipt) ⇔ Pushover definitively answered about _this receipt_:
> HTTP 2xx success, or any 4xx **except 429**.
> **Transient** (`False`, retry next poll) ⇔ the answer may change: network error, 5xx,
> **429**, or a malformed/non-JSON response.

Change sketch (one guard ahead of the existing 4xx branch in `cancel()`):

```python
if response.status_code == 429:
    # Rate limit / quota: transient — the receipt still exists, retry next poll.
    logger.warning("Pushover cancel for %s rate-limited (HTTP 429); will retry.", path)
    return False
if 400 <= response.status_code < 500:
    ...  # unchanged: settled, receipt gone
```

Considered and excluded: adding **408** (Request Timeout) to the transient set. Real timeouts
surface as `requests` exceptions (already transient); a server-_sent_ 408 from Pushover is
speculative. One status, one reason — keep the diff four lines.

## 4. Why this cannot recreate the 404 loop

The Session 8 pathology was an _unbounded_ retry of a _permanent_ outcome. A transient 429 is
bounded structurally: `retry_pending_cancels` fires once per poll (default 60 s), and the receipt
expires server-side at `emergency_expire` (≤ 3 h) — after which Pushover answers the retry with
**404, which settles it** under the unchanged 4xx rule. Worst case is ~180 cheap retries and a
guaranteed clean exit via the 404 backstop. No forever-loop is reachable.

## 5. Edge cases

- **`Retry-After` header — do not honor it.** Honoring would require timestamped
  `pending_cancel` entries (a state-schema migration) to skip polls, for zero practical gain:
  the 60 s poll cadence is already a generous backoff, and the 404 backstop caps total duration
  regardless of how large `Retry-After` is. Simplicity-first.
- **Quota exhaustion vs burst rate-limit:** indistinguishable at the wire (both 429) and need no
  distinction — identical retry-till-404 behavior. Note the system already _pre-announces_ the
  quota case: `low_quota` opens at 500 remaining (`incident_manager.py:39`,
  `ALERT_DEDUPLICATION_PROPOSAL.md` §9.2), so an operator is paged well before cancel-429s can
  begin.
- **`send()` / `check_receipt()` on 429:** already correct — `None` / `{}` mean "failed this
  poll / no new information," which is the right transient handling; no change.
- **`cancel_by_tag()` asymmetry stays:** a 4xx there returns `False` (permanent classed as
  transient), which is safe because its consumer never loops. Documented in the test plan (D2);
  aligning it would add code for no behavioral gain.

## 6. Test impact on the F4 plan (~27 cases)

- **C2** (was: parametrize 400/410/429 → all settle): becomes **400/410 → `True`**.
- **New C2b:** 429 → `False` + a single **WARNING** (not ERROR, not the misleading
  "receipt already gone" INFO).
- **New C4 (backstop property):** same receipt answers 429 (→ `False`, retained) then 404
  (→ `True`, settled) — encodes §4 so the loop-safety argument is executable.
- Net: **+2 cases (~29 total)**; groups A/B/D/E/F/G unchanged.

## 7. Migration and sequencing

- **No state migration** (`pending_cancel` stays `list[str]`), **no config**, no API-visible
  change. Behavior differs only while Pushover is actually rate-limiting.
- **Sequencing recommendation: bundle with the F4 test implementation in one session.** The wire
  tests don't exist yet — landing this first means C2/C2b/C4 encode the _new_ contract from day
  one instead of being written twice. Effort as a bundle rider: **XS** (~4 lines of production
  code + the 2 extra tests).
- **Docs:** supersedes the Session 8 "all 4xx settle" decision — one line in the PR description
  and in the `IMPLEMENTATION_LOG.md` closing entry (Backlog item, audit F9). No edit needed to
  `ALERT_DEDUPLICATION_PROPOSAL.md` §6 — "transient network blip → retry" already expresses the
  intent; 429 simply joins the transient set.

## 8. Open questions for the review round

1. Accept 429 → transient as scoped (single status, `cancel()` only)? **Recommend yes.**
2. Log level for the 429 line — WARNING once per poll for ≤ 3 h worst case acceptable, or prefer
   INFO? **Recommend WARNING** (an operator mid-quota-crisis should see it).
3. Bundle with F4's test session vs separate PR? **Recommend bundle** (§7).

## 9. Assumptions

Pushover's 429 semantics (monthly message limit exceeded and/or burst rate-limit, both
time-recoverable) are per their public API docs; worth a 30-second re-check of
<https://pushover.net/api#limits> during implementation. The claim does not affect the design:
whatever a Pushover 429 means, it is never "this receipt permanently ceased to exist" — 404/410
carry that meaning.
