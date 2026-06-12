# Proposal: Pushover Glances — At-a-Glance Farm Progress Widget

- **Date:** 2026-06-11; **revised 2026-06-12**
- **Status:** **SHELVED** — see correction below. Superseded for now by `STATUS_PAGE_PROPOSAL.md`.

---

## Correction (2026-06-12): Glances display is Apple Watch–only

The first revision of this proposal described Glances as rendering on "phone lock-screen widgets (iOS/Android) and Apple Watch complications." That overstated current support. The Glances API documentation states plainly:

> "Note: at this time, the Apple Watch is the only supported widget." … "Android and iOS widgets will be supported in future versions of our apps."

So today, Glances data is **only visible on an Apple Watch complication**. The author does not own an Apple Watch, which removes the entire value of the feature for this project. The phone-widget support that would make it worthwhile is "future versions" with no date.

## Decision

Shelve. Do not implement. Revisit only if either becomes true:

1. Pushover ships iOS/Android phone-widget support for Glances (watch their changelog/blog), or
2. An Apple Watch enters the picture.

The underlying need — "check farm progress on demand without opening `database.json`/`shortened.json`" — is real and is now addressed by the local status page proposed in **`STATUS_PAGE_PROPOSAL.md`**.

## Preserved design notes (for if/when this is revived)

The design from rev. 1 remains sound if the platform gap closes; keeping the essentials so it doesn't have to be re-derived:

- **API:** `POST https://api.pushover.net/1/glances.json` with `token`/`user`; fields `title` (≤100), `text` (≤100), `subtext` (≤100), `count` (int), `percent` (0–100).
- **The natural mapping:** `percent = min(100, battle_count)` — the 0–100 Master-color grind is literally a progress ring; `text = "Manon 96/100"`; `subtext` = finished tally (`14/26 done`) or live incident status (`STUCK 12m`).
- **Constraints:** watchOS budget ~50 updates/day, ≥20 min between updates recommended, delivery lag up to ~10 min. Hence: change-driven updates with a ≥30 min spacing floor (48/day worst case).
- **Failure handling:** cosmetic feature — log at DEBUG, never retry eagerly, never alert, never raise; rides on the `PushoverClient` never-raise contract from `ALERT_DEDUPLICATION_PROPOSAL.md`.
- **Unverified:** whether Glance calls count against the 10,000 msg/month quota (docs silent; check `X-Limit-App-*` headers empirically).
- **Config:** `glances_enabled`, default false (user must also add the widget/complication device-side).
