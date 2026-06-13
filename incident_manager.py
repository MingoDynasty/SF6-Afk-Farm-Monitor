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

logger = logging.getLogger(__name__)

# Step 6 relocates this (and database.json) into a data/ directory; for now it
# lives alongside database.json in the repo root.
NOTIFICATION_STATE_FILENAME = Path("notification_state.json")

STUCK_FARM = "stuck_farm"
API_DOWN = "api_down"
STUCK_FARM_TAG = "sf6mon-stuck_farm"


class IncidentManager:
    """Runs the §2 state machine and owns ``notification_state.json``."""

    def __init__(
        self,
        client: PushoverClient,
        config: ConfigData,
        state_path: str | Path = NOTIFICATION_STATE_FILENAME,
        clock: Callable[[], float] = time.time,
    ) -> None:
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
            self.client.cancel_by_tag(STUCK_FARM_TAG)
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
        self.last_change_at = self.clock()
        self._save()

    def seconds_since_last_change(self) -> float:
        return self.clock() - self.last_change_at

    # -- stuck_farm incident (emergency priority) ---------------------------

    def evaluate_stuck_farm(
        self, active: bool, build_message: Callable[[], str]
    ) -> None:
        incident = self.incidents.get(STUCK_FARM)
        if active:
            if incident is None:
                self._open_stuck_farm(build_message)
            else:
                self._maintain_stuck_farm(incident, build_message)
        elif incident is not None:
            self._close_stuck_farm(incident)

    def _send_emergency(self, message: str) -> str | None:
        return self.client.send(
            message,
            priority=2,
            retry=self.config.emergency_retry,
            expire=self.config.emergency_expire,
            tags=STUCK_FARM_TAG,
        )

    def _open_stuck_farm(self, build_message: Callable[[], str]) -> None:
        message = build_message()
        receipt: str | None = None
        if self.enabled:
            receipt = self._send_emergency(message)
            if receipt is None:
                logger.error("stuck_farm emergency send failed; will retry next poll.")
                return
        self.incidents[STUCK_FARM] = {
            "receipt": receipt,
            "opened_at": self.clock(),
            "expire": self.config.emergency_expire,
            "acked_at": 0,
        }
        logger.warning("stuck_farm incident OPENED: %s", message)
        self._save()

    def _maintain_stuck_farm(
        self, incident: dict[str, Any], build_message: Callable[[], str]
    ) -> None:
        # Still stuck and already OPEN: send nothing unless a re-raise/re-arm
        # policy fires. With Pushover disabled there is no receipt to inspect.
        if not self.enabled or incident.get("receipt") is None:
            logger.debug("stuck_farm still active; incident OPEN, staying silent.")
            return

        now = self.clock()
        acked_at = incident.get("acked_at") or 0
        if acked_at:
            # Re-arm after ack: an acknowledged-but-unrecovered farm is re-paged
            # re_alert_after_ack seconds later (§4). 0 disables it.
            re_alert_after_ack = self.config.re_alert_after_ack
            if re_alert_after_ack > 0 and now - acked_at >= re_alert_after_ack:
                self._reraise_stuck_farm(incident, build_message, "re-arm after ack")
            return

        # Not yet acknowledged: poll the receipt. A failed check is "no new
        # information" -> stay OPEN and silent (§6).
        info = self.client.check_receipt(incident["receipt"])
        if not info:
            return
        if info.get("acknowledged") == 1:
            incident["acked_at"] = info.get("acknowledged_at") or now
            logger.info(
                "stuck_farm alert acknowledged; re-arm timer started "
                "(still OPEN until recovery is observed)."
            )
            self._save()
            return

        # Un-acknowledged: re-raise once the alert has expired locally
        # (expires_at = opened_at + expire), per §4.
        if now >= incident["opened_at"] + incident["expire"]:
            self._reraise_stuck_farm(
                incident, build_message, "re-raise on un-acked expiry"
            )

    def _reraise_stuck_farm(
        self, incident: dict[str, Any], build_message: Callable[[], str], reason: str
    ) -> None:
        receipt = self._send_emergency(build_message())
        if receipt is None:
            logger.error("stuck_farm %s send failed; will retry next poll.", reason)
            return
        incident["receipt"] = receipt
        incident["opened_at"] = self.clock()
        incident["expire"] = self.config.emergency_expire
        incident["acked_at"] = 0
        logger.warning("stuck_farm incident %s; fresh emergency alert sent.", reason)
        self._save()

    def _close_stuck_farm(self, incident: dict[str, Any]) -> None:
        receipt = incident.get("receipt")
        if self.enabled and receipt is not None:
            if not self.client.cancel(receipt):
                # Keep the receipt and retry the cancel next poll (§6); the only
                # cost of a failed cancel is continued nagging until ack/expire.
                self.pending_cancel.append(receipt)
                logger.warning(
                    "stuck_farm recovery: receipt cancel failed; "
                    "will retry cancel next poll."
                )
        del self.incidents[STUCK_FARM]
        logger.info("stuck_farm incident CLOSED (recovery observed).")
        self._save()

    # -- api_down incident (one-shot, high priority) ------------------------

    def evaluate_api_down(self, active: bool, down_message: str | None = None) -> None:
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
            self.client.send(down_message, priority=1)
        self.incidents[API_DOWN] = {"opened_at": self.clock()}
        logger.warning("api_down incident OPENED: %s", down_message)
        self._save()

    def _close_api_down(self, incident: dict[str, Any]) -> None:
        now = self.clock()
        outage = timedelta(seconds=now - (incident.get("opened_at") or now))
        message = f"Capcom Buckler API recovered after {humanize.precisedelta(outage)}."
        if self.enabled:
            self.client.send(message, priority=0)
        del self.incidents[API_DOWN]
        logger.info("api_down incident CLOSED (recovery observed): %s", message)
        self._save()
