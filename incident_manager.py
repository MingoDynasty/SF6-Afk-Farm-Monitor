"""
Incident lifecycle / alert deduplication.

Implements the edge-triggered incident state machine from
``ALERT_DEDUPLICATION_PROPOSAL.md`` (§2, §4, §6): at most one open incident per
alert type, a message is sent only on the CLOSED -> OPEN transition, and an
incident closes only when the app observes recovery (never on acknowledgement).
Owns ``notification_state.json``.
"""

import json
import logging
import os
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import humanize

from config import ConfigData
from notifier_client import PushoverClient
from paths import DATA_DIR

logger = logging.getLogger(__name__)

NOTIFICATION_STATE_FILENAME = DATA_DIR / "notification_state.json"

STUCK_FARM = "stuck_farm"
API_DOWN = "api_down"
AUTH_EXPIRED = "auth_expired"
SWAP_NEEDED = "swap_needed"
LOW_QUOTA = "low_quota"

# Open a one-shot low-quota incident when fewer than this many Pushover messages
# remain in the monthly allowance, so quota exhaustion never silently mutes real
# alerts (§9.2). Closes when the count rises back above the floor (monthly reset).
QUOTA_FLOOR = 500
STUCK_FARM_TAG = "sf6mon-stuck_farm"
AUTH_EXPIRED_TAG = "sf6mon-auth_expired"
SWAP_NEEDED_TAG = "sf6mon-swap_needed"

# Emergency incident types share one state machine (open / maintain / re-raise /
# re-arm / close on observed recovery); each carries its own Pushover tag so
# startup reconciliation can cancel dangling server-side retries by tag (§6).
EMERGENCY_TAGS = {
    STUCK_FARM: STUCK_FARM_TAG,
    AUTH_EXPIRED: AUTH_EXPIRED_TAG,
    SWAP_NEEDED: SWAP_NEEDED_TAG,
}

# Distinct built-in Pushover sound per incident type so alerts are
# distinguishable from the lock screen without reading them (§9.1).
INCIDENT_SOUNDS = {
    STUCK_FARM: "pushover",
    SWAP_NEEDED: "magic",
    AUTH_EXPIRED: "falling",
    API_DOWN: "falling",
}

# Incident types that deep-link to the monitored profile's play page (§9.3):
# one tap from the notification to verification. Only the two actionable
# farm-progress alerts carry it; auth/api/quota alerts are not page-specific.
PROFILE_LINK_KINDS = frozenset({STUCK_FARM, SWAP_NEEDED})
PROFILE_LINK_TITLE = "Open Buckler profile"


class IncidentManager:
    """Runs the §2 state machine and owns ``notification_state.json``."""

    def __init__(
        self,
        client: PushoverClient,
        config: ConfigData,
        state_path: str | Path = NOTIFICATION_STATE_FILENAME,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Load persisted incident state and prepare notification handling."""
        self.client = client
        self.config = config
        self.enabled = config.pushover_enabled
        self.state_path = Path(state_path)
        self.clock = clock

        self.incidents: dict[str, dict[str, Any]] = {}
        self.last_change_at: float = 0.0
        self.pending_cancel: list[str] = []
        self._needs_reconcile = self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> bool:
        """Load state from disk. Returns True if it was missing or corrupt."""
        try:
            with self.state_path.open(encoding="utf-8") as file:
                data = json.load(file)
        except FileNotFoundError:
            self._init_fresh()
            return True
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "Could not read %s (%s); rebuilding notification state.",
                self.state_path,
                exc.__class__.__name__,
            )
            self._init_fresh()
            return True

        if not isinstance(data, dict):
            logger.error(
                "%s did not contain a JSON object; rebuilding notification state.",
                self.state_path,
            )
            self._init_fresh()
            return True

        self.incidents = data.get("incidents") or {}
        self.pending_cancel = list(data.get("pending_cancel") or [])
        self.last_change_at = float(data.get("last_change_at") or 0)
        if self.last_change_at <= 0:
            self.last_change_at = self.clock()
        return False

    def _init_fresh(self) -> None:
        self.incidents = {}
        self.pending_cancel = []
        self.last_change_at = self.clock()

    def _save(self) -> None:
        data = {
            "incidents": self.incidents,
            "last_change_at": self.last_change_at,
            "pending_cancel": self.pending_cancel,
        }
        temporary_path = self.state_path.with_name(f"{self.state_path.name}.tmp")
        with temporary_path.open("w", encoding="utf-8") as file:
            json.dump(data, file, indent=2)
            file.write("\n")
        os.replace(temporary_path, self.state_path)

    # -- startup / housekeeping ---------------------------------------------

    def reconcile_on_startup(self) -> None:
        """If the state file was missing/corrupt, cancel dangling server-side
        emergency retries by tag so a crash leaves at most one duplicate alert
        (§6)."""
        if not self._needs_reconcile:
            return
        logger.warning(
            "Notification state was missing or corrupt; "
            "cancelling any dangling emergency retries by tag."
        )
        if self.enabled:
            for tag in EMERGENCY_TAGS.values():
                if not self.client.cancel_by_tag(tag):
                    # A transient failure here can leave server-side emergency
                    # retries dangling until ack/expire (bounded, self-healing).
                    # We do NOT park the tag for a later retry: tags are
                    # per-incident-kind and reused, so a deferred cancel_by_tag
                    # could cancel a *future* real incident's retries. Surface it
                    # instead. See docs/PR_REVIEW_PUSHBACK.md (P3).
                    logger.warning(
                        "cancel_by_tag(%s) failed during startup reconciliation; "
                        "any dangling emergency retries persist until acknowledged "
                        "or expired.",
                        tag,
                    )
        self._save()

    def retry_pending_cancels(self) -> None:
        """Retry receipt cancels that failed on a previous poll (§6)."""
        if not self.pending_cancel or not self.enabled:
            return
        still_pending = [
            receipt
            for receipt in self.pending_cancel
            if not self.client.cancel(receipt)
        ]
        if still_pending != self.pending_cancel:
            self.pending_cancel = still_pending
            self._save()

    # -- stuck-timer state (replaces the database.json mtime check, M10) -----

    def record_change(self) -> None:
        """Record farm progress and persist the updated change time."""
        self.last_change_at = self.clock()
        self._save()

    def seconds_since_last_change(self) -> float:
        """Return elapsed seconds since the latest observed farm progress."""
        return self.clock() - self.last_change_at

    # -- emergency incidents (stuck_farm, auth_expired) ---------------------

    def evaluate_stuck_farm(
        self, active: bool, build_message: Callable[[], str]
    ) -> None:
        """Open, update, or close the stuck-farm emergency incident."""
        self._evaluate_emergency(STUCK_FARM, STUCK_FARM_TAG, active, build_message)

    def evaluate_auth_expired(
        self, active: bool, build_message: Callable[[], str]
    ) -> None:
        """Open, update, or close the expired-session emergency incident."""
        # Same emergency policy as stuck_farm (retry/expire/re-raise/re-arm):
        # expired Buckler cookies are fully actionable and blind all monitoring
        # until fixed. The only difference is the close signal — auth_expired
        # closes on the first successful poll, which do_task drives via the
        # active flag (review finding M3).
        self._evaluate_emergency(AUTH_EXPIRED, AUTH_EXPIRED_TAG, active, build_message)

    def evaluate_swap_needed(
        self,
        increased_characters: list[str],
        crossed_characters: list[str],
        build_message: Callable[[str], str],
    ) -> None:
        """Track whether a completed character still needs to be swapped."""
        # Master-color swap incident (§7), replacing the per-match re-fire from
        # commit ffb650b. Emergency policy identical to stuck_farm; the only
        # differences are the open and close signals:
        #   open  = a character's count crosses 100 this poll
        #   close = a *different* character's count starts increasing (the user
        #           actually swapped). Continued matches on the finished
        #           character keep the incident OPEN and silent (modulo re-arm).
        incident = self.incidents.get(SWAP_NEEDED)
        if incident is not None:
            # Always present: set via ``extra`` when the swap incident is opened.
            finished = incident["character"]
            if not any(character != finished for character in increased_characters):
                # The finished character is still gaining (or the poll is flat):
                # stay OPEN and silent, modulo re-raise/re-arm.
                self._maintain_emergency(
                    SWAP_NEEDED,
                    SWAP_NEEDED_TAG,
                    incident,
                    lambda: build_message(finished),
                )
                return
            # A different character started gaining: the swap happened. Close
            # this incident, then fall through to the open check — if that same
            # poll *also* crossed 100 on another character (e.g. the user swapped
            # onto a character already sitting at 99), its incident must open
            # now. The crossing is an edge that is gone after this poll's
            # database write, so it cannot be recovered on a later poll.
            self._close_emergency(SWAP_NEEDED, incident)

        if crossed_characters:
            finished = crossed_characters[0]
            self._open_emergency(
                SWAP_NEEDED,
                SWAP_NEEDED_TAG,
                lambda: build_message(finished),
                extra={"character": finished},
                # The 100-crossing is an edge: if the open send fails it cannot
                # be retried next poll, so record a receipt-less incident and let
                # _maintain_emergency page on the next poll (P1).
                record_on_send_failure=True,
            )

    def _evaluate_emergency(
        self, kind: str, tag: str, active: bool, build_message: Callable[[], str]
    ) -> None:
        incident = self.incidents.get(kind)
        if active:
            if incident is None:
                self._open_emergency(kind, tag, build_message)
            else:
                self._maintain_emergency(kind, tag, incident, build_message)
        elif incident is not None:
            self._close_emergency(kind, incident)

    def _profile_url(self) -> str:
        return (
            "https://www.streetfighter.com/6/buckler/profile/"
            f"{self.config.user_code}/play"
        )

    def _send_emergency(self, kind: str, message: str, tag: str) -> str | None:
        url = self._profile_url() if kind in PROFILE_LINK_KINDS else None
        url_title = PROFILE_LINK_TITLE if url else None
        return self.client.send(
            message,
            priority=2,
            retry=self.config.emergency_retry,
            expire=self.config.emergency_expire,
            tags=tag,
            sound=INCIDENT_SOUNDS.get(kind),
            url=url,
            url_title=url_title,
            timestamp=int(self.clock()),
        )

    def _open_emergency(
        self,
        kind: str,
        tag: str,
        build_message: Callable[[], str],
        extra: dict[str, Any] | None = None,
        record_on_send_failure: bool = False,
    ) -> None:
        message = build_message()
        receipt: str | None = None
        if self.enabled:
            receipt = self._send_emergency(kind, message, tag)
            if receipt is None and not record_on_send_failure:
                # Level-triggered incidents (stuck_farm, auth_expired) recover a
                # failed open for free: the condition is still active next poll,
                # so the open is simply retried. Record nothing now.
                logger.error("%s emergency send failed; will retry next poll.", kind)
                return
            if receipt is None:
                # Edge-triggered incidents (swap_needed) have no next-poll edge
                # to retry on — the crossing is gone once do_task writes the new
                # count — so record a receipt-less incident. _maintain_emergency
                # sends the first real alert on the next poll instead (P1).
                logger.error(
                    "%s emergency send failed; recorded without a receipt, "
                    "will send on the next poll.",
                    kind,
                )
        incident: dict[str, Any] = {
            "receipt": receipt,
            "opened_at": self.clock(),
            "expire": self.config.emergency_expire,
            "acked_at": 0,
        }
        if extra:
            incident.update(extra)
        self.incidents[kind] = incident
        logger.warning("%s incident OPENED: %s", kind, message)
        self._save()

    def _maintain_emergency(
        self,
        kind: str,
        tag: str,
        incident: dict[str, Any],
        build_message: Callable[[], str],
    ) -> None:
        # Still active and already OPEN: send nothing unless a re-raise/re-arm
        # policy fires.
        if not self.enabled:
            # Pushover disabled: no receipt to inspect, nothing to send.
            logger.debug("%s still active; Pushover disabled, staying silent.", kind)
            return
        if incident.get("receipt") is None:
            # A receipt-less incident never paged anyone — it was opened either
            # while Pushover was disabled (now re-enabled, P2) or, for the
            # edge-triggered swap_needed, after a failed open send that was
            # recorded anyway (P1). Pushover is enabled and the condition still
            # holds, so send the first real emergency alert and attach a receipt.
            self._reraise_emergency(
                kind,
                tag,
                incident,
                build_message,
                "first alert for a receipt-less incident",
            )
            return

        now = self.clock()
        acked_at = incident.get("acked_at") or 0
        if acked_at:
            # Re-arm after ack: an acknowledged-but-unrecovered incident is
            # re-paged re_alert_after_ack seconds later (§4). 0 disables it.
            re_alert_after_ack = self.config.re_alert_after_ack
            if re_alert_after_ack > 0 and now - acked_at >= re_alert_after_ack:
                self._reraise_emergency(
                    kind, tag, incident, build_message, "re-arm after ack"
                )
            return

        # Not yet acknowledged: poll the receipt. A failed check is "no new
        # information" -> stay OPEN and silent (§6).
        info = self.client.check_receipt(incident["receipt"])
        if not info:
            return
        if info.get("acknowledged") == 1:
            incident["acked_at"] = info.get("acknowledged_at") or now
            logger.info(
                "%s alert acknowledged; re-arm timer started "
                "(still OPEN until recovery is observed).",
                kind,
            )
            self._save()
            return

        # Un-acknowledged: re-raise once the alert has expired locally
        # (expires_at = opened_at + expire), per §4.
        if now >= incident["opened_at"] + incident["expire"]:
            self._reraise_emergency(
                kind, tag, incident, build_message, "re-raise on un-acked expiry"
            )

    def _reraise_emergency(
        self,
        kind: str,
        tag: str,
        incident: dict[str, Any],
        build_message: Callable[[], str],
        reason: str,
    ) -> None:
        receipt = self._send_emergency(kind, build_message(), tag)
        if receipt is None:
            logger.error("%s %s send failed; will retry next poll.", kind, reason)
            return
        incident["receipt"] = receipt
        incident["opened_at"] = self.clock()
        incident["expire"] = self.config.emergency_expire
        incident["acked_at"] = 0
        logger.warning("%s incident %s; fresh emergency alert sent.", kind, reason)
        self._save()

    def _close_emergency(self, kind: str, incident: dict[str, Any]) -> None:
        receipt = incident.get("receipt")
        if self.enabled and receipt is not None:
            if not self.client.cancel(receipt):
                # Keep the receipt and retry the cancel next poll (§6); the only
                # cost of a failed cancel is continued nagging until ack/expire.
                self.pending_cancel.append(receipt)
                logger.warning(
                    "%s recovery: receipt cancel failed; will retry cancel next poll.",
                    kind,
                )
        del self.incidents[kind]
        logger.info("%s incident CLOSED (recovery observed).", kind)
        self._save()

    # -- api_down incident (one-shot, high priority) ------------------------

    def evaluate_api_down(self, active: bool, down_message: str | None = None) -> None:
        """Open or close the one-shot Buckler API outage incident."""
        incident = self.incidents.get(API_DOWN)
        if active:
            if incident is None:
                self._open_api_down(down_message or "Capcom Buckler API unreachable.")
            # Already OPEN: one-shot, send nothing more.
        elif incident is not None:
            self._close_api_down(incident)

    def _open_api_down(self, down_message: str) -> None:
        if self.enabled:
            # priority=1 returns no receipt, so the return value is not a
            # success signal; this is a best-effort one-shot send.
            self.client.send(
                down_message,
                priority=1,
                sound=INCIDENT_SOUNDS.get(API_DOWN),
                timestamp=int(self.clock()),
            )
        self.incidents[API_DOWN] = {"opened_at": self.clock()}
        logger.warning("api_down incident OPENED: %s", down_message)
        self._save()

    def _close_api_down(self, incident: dict[str, Any]) -> None:
        now = self.clock()
        outage = timedelta(seconds=now - (incident.get("opened_at") or now))
        message = f"Capcom Buckler API recovered after {humanize.precisedelta(outage)}."
        if self.enabled:
            self.client.send(message, priority=0, timestamp=int(now))
        del self.incidents[API_DOWN]
        logger.info("api_down incident CLOSED (recovery observed): %s", message)
        self._save()

    # -- low_quota incident (one-shot, high priority) -----------------------

    def evaluate_low_quota(self) -> None:
        """Open a one-shot alert when the Pushover monthly quota runs low (§9.2).

        Reads the latest ``X-Limit-App-Remaining`` the client captured on any
        send/receipt/cancel this poll. ``None`` means no Pushover call has been
        made yet (e.g. a healthy poll that sent nothing, or Pushover disabled),
        so there is no quota information to act on.
        """
        remaining = self.client.last_remaining
        if remaining is None:
            return
        incident = self.incidents.get(LOW_QUOTA)
        if remaining < QUOTA_FLOOR:
            if incident is None:
                self._open_low_quota(remaining)
            # Already OPEN: one-shot, send nothing more.
        elif incident is not None:
            self._close_low_quota(remaining)

    def _open_low_quota(self, remaining: int) -> None:
        message = (
            f"Pushover quota low: only {remaining} messages left this month. "
            "Real alerts will be dropped once it hits zero."
        )
        if self.enabled:
            # priority=1 returns no receipt; best-effort one-shot send.
            self.client.send(message, priority=1, timestamp=int(self.clock()))
        self.incidents[LOW_QUOTA] = {"opened_at": self.clock()}
        logger.warning("low_quota incident OPENED: %s", message)
        self._save()

    def _close_low_quota(self, remaining: int) -> None:
        # Recovery (monthly reset) is silent: announcing it would itself spend a
        # message, and there is nothing for the user to act on.
        del self.incidents[LOW_QUOTA]
        logger.info(
            "low_quota incident CLOSED: quota recovered to %s messages.", remaining
        )
        self._save()
