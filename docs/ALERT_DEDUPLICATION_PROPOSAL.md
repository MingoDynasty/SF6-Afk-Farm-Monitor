# Proposal: Alert Deduplication via Incident Lifecycle + Pushover Emergency Priority

- **Date:** 2026-06-11 (rev. 2 — incorporates author feedback; see §11 decisions log)
- **Status:** Draft for discussion
- **Related:** `CODEBASE_REVIEW.md` findings M2 (notification spam / quota exhaustion), H2 (send failures crash app), H4 (chump DEBUG credential leak), M10 (mtime-as-state fragility); the existing TODO in `notifier_client.py:14-20`, which already sketches the emergency-message idea. Glances widget support is covered separately in `GLANCES_PROPOSAL.md`.

---

## 1. Problem

When a continuous failure condition holds (stuck farm, Capcom API down), `do_task` re-sends the same Pushover message on **every poll** — one message per minute at default settings. Consequences:

- The user is spammed with identical notifications.
- A multi-day incident burns through Pushover's 10,000 messages/month free quota (~7 days at 1/min), after which *all* future alerts — including new, real ones — are rejected.
- There is no notion of "this is the same ongoing problem" vs "this is a new problem."

Desired behavior: **exactly one alert per unique incident.** While an incident is unresolved, don't re-send it. Once it's resolved, the next occurrence is a new incident and may alert again.

## 2. Key reframe: don't deduplicate sends — manage incidents

The straw-man implementation ("before sending, call Pushover to check whether the previous alert is still unacknowledged") works, but it keeps the app in the business of deciding *when to re-notify the human*. Pushover already solves that half of the problem natively:

> An **emergency-priority message** (`priority=2`, with `retry` and `expire` parameters) is re-delivered by Pushover's own servers every `retry` seconds (minimum 30, **capped at 50 total retries**) until the user **acknowledges** it or `expire` seconds pass (maximum 10,800 = 3 hours). The send returns a `receipt`; `GET /1/receipts/{receipt}.json` reports `acknowledged`, `acknowledged_at`, `expired`, etc. (pollable up to 1 week, no faster than once per 5 s); `POST /1/receipts/{receipt}/cancel.json` stops the retries early.

So the app's job collapses to maintaining a tiny piece of state — **at most one open incident per alert type** — and three transitions:

```
                 condition observed,
                 no open incident
   ┌────────┐ ──────────────────────► ┌──────────────────────────┐
   │ CLOSED │   send priority=2 msg   │ OPEN (holding a receipt) │
   │        │   store receipt         │ Pushover nags the user   │
   └────────┘ ◄────────────────────── └──────────────────────────┘
                 recovery observed             │
                 (cancel receipt,              │ condition still true:
                  close incident)              ▼ do NOTHING locally
                                       (Pushover is already retrying;
                                        no new messages are sent)
```

- **OPEN → stays OPEN while the condition holds.** The app sends nothing. Pushover handles re-alerting the human (that's what `retry` is for).
- **OPEN → CLOSED only when the app *observes recovery*** (for the stuck-farm incident: a battle count changed again; for API-down: a poll succeeded). On close, call the cancel endpoint so the nagging stops immediately — even if the user never acknowledged (e.g., the farm un-stuck itself when an opponent finally connected). This "auto-cancel on recovery" is a feature the current spam approach can't offer at all.
- **Acknowledgement does *not* close the incident.** This is the crux — see §3.

This is exactly the design the existing TODO in `notifier_client.py` gestures at ("1 message per 1 incident"), made concrete.

## 3. The ack-timing-window problem, and why "close on recovery" solves it

The concern raised: user acknowledges/clears the alert, but fixing the farm takes time — find a new match, finish it — and until that first match completes, the data still hasn't changed. Won't the app, seeing a still-stale DB on its next poll, fire a fresh alert?

**No — because under this proposal, a stale DB by itself can never cause a send.** The mental model shift is from *level-triggered* to *edge-triggered* alerts:

- **Today (level-triggered):** every poll asks "is the DB stale?" → if yes, send. Staleness *is* the send trigger, so it fires every poll.
- **Proposed (edge-triggered):** staleness is only the *condition input* to the state machine in §2. A message is sent on exactly one transition: **CLOSED → OPEN**. While the incident is OPEN, polls that still see a stale DB conclude "condition still active, incident already open" and do nothing. And the incident can only return to CLOSED by *observing a count change* — never by an ack. So a second alert structurally requires the farm to have visibly recovered first.

Concrete timeline (defaults: 60 s poll, 6 min stuck timeout, `retry`=120 s):

| Time | Event | Incident state | Messages sent by app |
|---|---|---|---|
| 12:00 | Farm stalls (opponent disconnect) | CLOSED | — |
| 12:06 | Poll sees counts stale ≥ timeout | CLOSED → **OPEN** | **1 emergency message** |
| 12:06–12:20 | Pushover's servers redeliver every 2 min (no app involvement) | OPEN | none |
| 12:20 | User acks on phone → Pushover stops redelivering | OPEN (acked) | none |
| 12:24 | User restarts the match queue | OPEN | none |
| 12:25, 12:26, … | Polls: counts *still* unchanged (match in progress) — **this is the feared window** | OPEN (no transition) | **none** |
| 12:31 | Match completes; a count increments | OPEN → **CLOSED** (receipt canceled — no-op, already acked) | none |
| 13:40 | Farm stalls again | CLOSED → **OPEN** | 1 new emergency message |

Ack and resolution are deliberately different events: **ack** means "a human saw it" and only silences Pushover's redelivery; **resolution** means "the app watched the condition end." The window between them is exactly the OPEN-and-silent stretch.

The only thing that can message you during that window is the *optional* re-arm reminder (§4), whose interval is deliberately chosen to exceed a worst-case match length — and it can be disabled.

### Why not the two alternatives suggested

**(a) "Factor in the time the user cleared the alert."** Partially adopted — but only for the optional re-arm policy in §4, not as the close signal. Using ack-time as the primary signal makes the app re-derive "is it really resolved?" from a human gesture, which is precisely the ambiguity that creates the timing window.

**(b) "On ack, blindly write the DB without comparison, so the mtime resets."** Recommend rejecting:

1. **It re-introduces the spam window it's trying to fix, just shifted by `battle_count_timeout`.** A blind write resets the stuck timer; if the post-fix match is still running 6 minutes after the ack (and the commit history shows exactly this — `4103f7d` raised the timeout *because* farmer-vs-farmer matches run long), the condition trips again and fires another alert while the user is mid-fix.
2. **It corrupts the meaning of `database.json`.** The file is "last observed battle counts," and its timestamp feeds the humanized "It has been (X) without an update" message. A write that records *nothing new* to silence a notification entangles notification state with monitoring data — the next stuck alert would report a wrong (shorter) stale duration.
3. It's not even less work: you still need to detect the ack via the receipts API to know *when* to do the blind write. Given that, keeping a separate, honest piece of notification state (§6) costs the same and lies to no one.

## 4. Policy knobs

"Close only on observed recovery" has one real downside: **ack-then-forget.** If the user acks at 2 AM, rolls over, and never fixes the farm, the incident stays OPEN and silent forever — the farm sits stuck all night with no further nag. Two cheap, local-clock-friendly policies cover it:

- **Re-raise on expiry (recommended, default on):** if the emergency message expires *unacknowledged* (user slept through up to 3 h of Pushover retries), send a fresh emergency message and replace the receipt. Cost: 1 message per 3 hours per ongoing incident — 8/day worst case vs today's 1,440/day. Note this needs no extra API polling: `expires_at` is just `opened_at + expire`, computable locally; one receipt check at that moment distinguishes "expired un-acked" (re-raise) from "acked" (don't).
- **Re-arm after ack (decided 2026-06-12: phase 1, default 10 min / 600 s):** if the incident is OPEN, *acked*, and still unresolved `re_alert_after_ack` seconds after `acknowledged_at`, send one fresh emergency message. Set `0` to disable.

  **Why an ack-timeout and not "as soon as possible" (discussed 2026-06-12):** the author proposed on-call semantics — ack without actual resolution should re-page ASAP. The on-call framing is correct, but real paging systems implement it as an **ack timeout** (ack = "I'm on it," buying a grace period; re-page only when that lapses), never as immediate re-page — because if acking doesn't buy quiet time to perform the fix, the ack is meaningless and the alert stream degenerates back into the §1 spam. In this system the grace period has a hard floor: the only observable proof of resolution is a completed match, so even a perfect fix looks "still stuck" for ~5–10 minutes after the ack (walk to PC + fix + re-queue + one full match). Pushover also offers no separate "resolved" action — ack is the only human signal, so the app cannot distinguish "on it" from "done."

  **Why 10 minutes:** author's pick — fix typically ≤5 min and a working farm completes a match roughly every ~5 min, so 10 covers fix + one match in the common case while re-paging an unfixed farm as fast as the model allows. Known tail case, accepted: a long match against another AFK farmer right after the fix (the same long-match scenario that made `4103f7d` raise `battle_count_timeout`) can push past 10 minutes and trigger one harmless false re-page while standing at the PC. It's a single config value (`re_alert_after_ack`) — tune after living with it.

### Choosing `retry` (how aggressively Pushover nags)

Pushover caps emergency redelivery at **50 retries**, so the effective nagging window per message is `min(expire, retry × 50)`. That makes `retry` a trade-off between *intensity* and *coverage*:

| `retry` | Cadence | Nagging stops after (un-acked) |
|---|---|---|
| 30 s (minimum) | buzz every 30 s | ~25 min, then silence until the 3 h expiry re-raise |
| 120 s (recommended) | buzz every 2 min | ~100 min |
| ≥216 s | buzz every ~3.6 min | spans the full 3 h `expire` window |

For the sleeping-user scenario, coverage beats intensity: a buzz every 2 minutes for over an hour and a half is more likely to eventually land than a frantic 25-minute burst followed by hours of silence. Hence the **120 s default**; bump toward 216 s if full-window coverage matters more, or down toward 30 s if alerts are usually handled while awake.

## 5. Does emergency priority bypass Do Not Disturb? (raised 2026-06-11)

Short answer: **not by itself — and that's a feature.** There are two separate "quiet" layers, and `priority=2` only affects the first:

1. **Pushover's own quiet hours** (configured inside the Pushover app): messages with priority ≥ 1 bypass these by design, per the API docs. The app's choice of priority controls this layer.
2. **The phone's OS-level Do Not Disturb / mute switch:** *no* Pushover priority bypasses this by default. Punch-through is an explicit, per-device, user-side opt-in:
   - **iOS:** Pushover holds Apple's **Critical Alerts** entitlement. Off by default; enabled in the Pushover app's settings. When enabled, high- and emergency-priority notifications bypass both the mute switch and DND, and play at a dedicated volume you configure (independent of the device volume).
   - **Android:** Pushover can play notification sounds **as alarms**, bypassing the mute setting; on newer Android versions you must additionally allow Pushover to override DND in the system notification settings (DND → Apps → add Pushover).

Implication for this design: `priority=2` governs *redelivery persistence* (Pushover keeps re-sending until ack), not *audibility through DND*. Whether an alert can wake you is decided once, on your device, by you — the app can't force it and doesn't need to care. So this is not a reason to avoid emergency priority; it's the supported path to "wake me up if I opted into that." **Deployment note:** for the AFK-farm use case to work overnight, enable Critical Alerts (iOS) or alarm-sound + DND override (Android) on the receiving device — worth a line in the README when this ships.

## 6. State, persistence, and crash recovery

A small `notification_state.json` (separate file — keep `database.json` purely "observed counts"; lives in the `data/` directory per the review's M8 addendum, decided 2026-06-12):

```json
{
  "incidents": {
    "stuck_farm": {
      "receipt": "rLqVuqTRYBupPyt9bxhmgloapuqMgc",
      "opened_at": 1781234567,
      "expire": 10800,
      "acked_at": 0
    }
  },
  "last_change_at": 1781230000
}
```

- **`last_change_at` replaces the `database.json` mtime hack** (review M10) as the stuck-timer source: updated whenever counts differ, compared against `time.time()` (no naive-datetime/DST issues, immune to backups/copies touching mtimes). This proposal is the natural vehicle for that fix since stuck-detection logic is being touched anyway.
- **Startup reconciliation:** send every emergency message with a `tags` parameter (e.g. `sf6mon-stuck_farm`). On startup, if the state file is missing/corrupt, `POST /1/receipts/cancel_by_tag/{tag}.json` clears any dangling server-side retries without needing the receipt, then state rebuilds from the next poll. Worst case after a crash: one duplicate alert. (This is also the answer to "non-atomic write corrupts the state file": self-healing, unlike `database.json` today.)
- **Cancel failures:** if recovery is observed but the cancel call fails (network blip), close the incident locally but keep a `pending_cancel` receipt to retry next poll; otherwise nagging continues until ack/expire — annoying, not harmful.
- **Receipt-check failures:** treat as "no new information" — incident stays OPEN, nothing is sent. Failing safe here means failing *quiet*, which is the right direction for a dedup feature.

## 7. Scope: which alerts become incidents

| Alert | Today | Proposed |
|---|---|---|
| Stuck farm | 1 msg/poll while stuck | **Incident**, emergency priority (actionable and time-sensitive — the entire point of the app) |
| Capcom API down / unreachable | 1 msg/poll while down | **Incident, one-shot `priority=1` (high)** — *decided 2026-06-11*: the user can't fix Capcom, so nagging until ack would be noise. Open → one message; close on first successful poll, optionally with a "recovered after X" courtesy message. |
| Buckler cookies expired (review M3, once implemented) | — (misreported today) | **Incident**, emergency or high — actionable (refresh cookies) and blocks all monitoring. |
| Master color finished (count crosses 100) | Intentionally re-fires every match past 100 (commit `ffb650b`) as a "swap characters!" nag | **Incident, emergency priority** — *decided 2026-06-11*: same shape as stuck-farm ("there is an issue, no progress until the user fixes it"). One nagging message replaces N quota-burning messages. **Resolution condition: a *different* character's count starts increasing** (i.e., the swap actually happened) — continued matches on the finished character keep the incident OPEN and silent (modulo re-arm). Scheduled for phase 2 since it replaces deliberate current behavior. |

`pushover_enabled = false`: incidents still open/close (keeps log behavior consistent) but no network calls; trivially degrades.

## 8. Prerequisites and implementation shape

Two review findings are hard prerequisites, and one is strongly synergistic:

1. **H1/H2 first:** this design *adds* network calls (receipt check, cancel) inside `do_task`. Without timeouts and exception-proofing, every new call is a new way to hang or crash the monitor.
2. **Replace `chump` with direct `requests` calls** as part of this work rather than around it. The feature needs: emergency send returning a receipt, receipt status, cancel, **and `cancel_by_tag`** — chump (abandonware, 2018) has no tag/cancel-by-tag support, no timeouts, and the DEBUG credential leak (H4). The whole Pushover surface needed here is ~40 lines over `requests`, which is already a dependency:

```
PushoverClient (notifier_client.py, rewritten)
  send(message, priority=0, retry=None, expire=None, tags=None,
       sound=None, url=None, url_title=None) -> receipt | None
  check_receipt(receipt) -> {acknowledged, acknowledged_at, expired, ...}
  cancel(receipt) -> bool
  cancel_by_tag(tag) -> bool
  # all with timeout=, all exceptions caught & logged, never raised to caller

IncidentManager (new module, owns notification_state.json)
  evaluate(type, condition_is_active, build_message) — runs the §2 state machine

task.do_task changes
  - compute booleans: api_down, stuck, (later: auth_expired, swap_needed)
  - hand each to IncidentManager.evaluate(...) instead of calling send_message directly
  - stuck timer reads last_change_at from state, not file mtime
```

Config additions (keep minimal): `emergency_retry` (default 120 s, min 30 per API), `emergency_expire` (default 10800, the max), `re_alert_after_ack` (default 600 s, `0` = disabled).

**Quota impact:** worst case drops from ~1,440 messages/day during an incident to ≤ ~8/day (re-raise every 3 h) + ~1 per incident open/close. Receipt checks and cancels are not message sends.

## 9. Other Pushover API features worth leveraging

Reviewed from https://pushover.net/api — in rough order of value for this app:

1. **Per-alert `sound`** — distinct built-in sounds per type (e.g. `siren` for stuck farm, `magic` for Master color finished, `falling` for API down). One parameter; makes alerts distinguishable from the lock screen without reading.
2. **Quota self-monitoring** — every message response carries `X-Limit-App-Limit` / `X-Limit-App-Remaining` / `X-Limit-App-Reset` headers (also `GET /1/apps/limits.json`). Log remaining on each send; open a one-shot incident when remaining drops below a floor (e.g. 500) so quota exhaustion is never a surprise. Cheap and directly addresses review M2's failure mode.
3. **`url` + `url_title`** — attach the Buckler profile link (`https://www.streetfighter.com/6/buckler/profile/{user_code}/play`) to alerts; one tap from the notification to verification.
4. **`GET /1/users/validate.json`** — validate `pushover_user_key` once at startup; fail fast with a clear log instead of discovering a typo'd key at first real alert.
5. **`timestamp`** — stamp alerts with detection time, so delayed deliveries display when the event actually happened.
6. **`html=1`** — minor polish (bold character names, colored "STUCK").
7. **Glances API** — silent lock-screen-widget / watch-complication progress display. *Accepted 2026-06-11*; spun out into **`GLANCES_PROPOSAL.md`**, scheduled after the high-priority review fixes and dedup phase 1.
8. **`ttl`** — auto-delete informational messages after N seconds (ignored for `priority=2`). Useful only if courtesy "recovered" messages are added; skip otherwise.
9. **`callback` webhook on ack — considered, rejected:** Pushover can POST to a public URL on acknowledgement, replacing receipt polling. Requires running a publicly reachable HTTPS endpoint, which is wildly out of proportion for this app; polling at the existing 60 s cadence is well within the API's 1-per-5-s receipt limit.

## 10. Alternatives considered

- **Tier 1 — local-only dedup, no receipts:** keep `priority=0`/`1`, track open/closed incidents purely in the local state file, send exactly one message per incident, close on observed recovery. Strictly simpler (no receipt/cancel/ack code at all) and kills 100% of the spam. What it gives up: no nagging for a sleeping user (one missable buzz for a stuck farm), no auto-stop of nagging (there is none), no ack-awareness. Given that the app's raison d'être is waking someone up when the AFK farm stalls, and the notifier TODO already wanted emergency semantics, the receipt-based design earns its extra ~60 lines. But if scope pressure hits, Tier 1 is a respectable fallback and is a strict subset of the proposed design (same state machine, fewer transitions).
- **Blind DB write on ack:** rejected — see §3(b).
- **Ack closes the incident:** rejected — re-creates the timing window; see §3.
- **Webhook callback instead of polling:** rejected — see §9.9.

## 11. Decisions log & remaining questions

Resolved 2026-06-11 with the author:

1. ~~"Capcom down" priority~~ — **decided:** one-shot high priority (`priority=1`), no nagging; we can't fix Capcom.
2. ~~Master color finished~~ — **decided:** convert to a nagging emergency incident (phase 2); resolution = a different character's count rises. Replaces the intentional re-fire behavior from `ffb650b`.
3. ~~DND interaction~~ — **resolved:** emergency priority does not bypass OS-level DND by default; bypass is a per-device opt-in (iOS Critical Alerts / Android alarm + DND override). See §5. Not a blocker; add a README deployment note.
4. ~~Glances~~ — **decided:** yes, after high-priority fixes; separate proposal in `GLANCES_PROPOSAL.md`.

Resolved 2026-06-12 with the author:

5. ~~Re-arm after ack~~ — **decided:** phase 1, default on at **600 s (10 min)**, framed as an on-call ack-timeout. The author first proposed re-paging ASAP after an unresolved ack (rejected — defeats the ack; reasoning in §4), then chose 10 min over the reviewer's 15 (trade-off documented in §4; tunable via config).
6. ~~`emergency_retry` default~~ — **decided:** 120 s (coverage over intensity given the 50-retry cap).

No open questions remain in this proposal.

## 12. Suggested phasing

- **Phase 0 (prereqs):** review H1 + H2 (timeouts, exception-proof sends); replace chump with the direct `PushoverClient` (also resolves H4, L1, L15).
- **Phase 1:** incident state machine for *stuck farm* (emergency) and *API down* (one-shot high); `notification_state.json` with `last_change_at` (retiring the mtime check, M10); emergency send/cancel/receipt; re-raise on expiry; re-arm after ack at 600 s (decided); `tags` + startup `cancel_by_tag`; config knobs; quota-header logging.
- **Phase 2:** Master-color swap incident (decided, §7); low-quota self-alert incident; auth-expiry incident (with M3's detection); sounds/url/timestamp polish; README deployment note on Critical Alerts / Android DND override (§5).
- **Phase 3 (optional, accepted):** Glances progress widget — see `GLANCES_PROPOSAL.md`.
