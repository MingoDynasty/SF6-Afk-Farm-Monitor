import json
from collections.abc import Callable
from pathlib import Path

from config import ConfigData
from conftest import FakeClock, FakePushoverClient
from incident_manager import (
    API_DOWN,
    AUTH_EXPIRED,
    AUTH_EXPIRED_TAG,
    STUCK_FARM,
    STUCK_FARM_TAG,
    IncidentManager,
)

STUCK_MESSAGE = (
    "It has been (5 minutes) without an update. The afk farm might be stuck."
)


def stuck_message() -> str:
    return STUCK_MESSAGE


def build_manager(
    fake_client: FakePushoverClient,
    make_config: Callable[..., ConfigData],
    fake_clock: FakeClock,
    tmp_path: Path,
    **overrides: object,
) -> IncidentManager:
    state_path = tmp_path / "notification_state.json"
    return IncidentManager(
        fake_client, make_config(**overrides), state_path, clock=fake_clock
    )


# -- stuck_farm: open / silent / recover --------------------------------------


def test_open_on_stuck_sends_exactly_one_emergency_message(
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
    tmp_path: Path,
) -> None:
    manager = build_manager(fake_client, make_config, fake_clock, tmp_path)

    manager.evaluate_stuck_farm(active=True, build_message=stuck_message)

    assert len(fake_client.sent) == 1
    sent = fake_client.sent[0]
    assert sent["priority"] == 2
    assert sent["retry"] == 120
    assert sent["expire"] == 10800
    assert sent["tags"] == STUCK_FARM_TAG
    assert sent["message"] == STUCK_MESSAGE
    assert STUCK_FARM in manager.incidents


def test_staleness_while_open_sends_nothing(
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
    tmp_path: Path,
) -> None:
    manager = build_manager(fake_client, make_config, fake_clock, tmp_path)
    manager.evaluate_stuck_farm(active=True, build_message=stuck_message)
    receipt = manager.incidents[STUCK_FARM]["receipt"]
    fake_client.receipt_info[receipt] = {"acknowledged": 0}

    # Many more polls, all still stale but well within the expiry window.
    for _ in range(5):
        fake_clock.advance(60)
        manager.evaluate_stuck_farm(active=True, build_message=stuck_message)

    assert len(fake_client.sent) == 1
    assert STUCK_FARM in manager.incidents


def test_receipt_check_failure_stays_open_and_silent(
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
    tmp_path: Path,
) -> None:
    manager = build_manager(fake_client, make_config, fake_clock, tmp_path)
    manager.evaluate_stuck_farm(active=True, build_message=stuck_message)

    # No receipt_info programmed => check_receipt returns {} => "no new info".
    fake_clock.advance(60)
    manager.evaluate_stuck_farm(active=True, build_message=stuck_message)

    assert len(fake_client.sent) == 1
    assert manager.incidents[STUCK_FARM]["acked_at"] == 0


def test_observed_recovery_closes_and_cancels_receipt(
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
    tmp_path: Path,
) -> None:
    manager = build_manager(fake_client, make_config, fake_clock, tmp_path)
    manager.evaluate_stuck_farm(active=True, build_message=stuck_message)
    receipt = manager.incidents[STUCK_FARM]["receipt"]

    manager.evaluate_stuck_farm(active=False, build_message=stuck_message)

    assert fake_client.cancelled == [receipt]
    assert STUCK_FARM not in manager.incidents
    assert manager.pending_cancel == []


def test_ack_alone_does_not_close_incident(
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
    tmp_path: Path,
) -> None:
    manager = build_manager(fake_client, make_config, fake_clock, tmp_path)
    manager.evaluate_stuck_farm(active=True, build_message=stuck_message)
    receipt = manager.incidents[STUCK_FARM]["receipt"]

    fake_clock.advance(60)
    ack_time = fake_clock.now
    fake_client.receipt_info[receipt] = {
        "acknowledged": 1,
        "acknowledged_at": ack_time,
    }
    manager.evaluate_stuck_farm(active=True, build_message=stuck_message)

    # Ack is recorded but the incident stays OPEN and nothing new is sent.
    assert STUCK_FARM in manager.incidents
    assert manager.incidents[STUCK_FARM]["acked_at"] == ack_time
    assert len(fake_client.sent) == 1
    assert fake_client.cancelled == []


def test_re_arm_fires_600s_after_ack_when_still_stuck(
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
    tmp_path: Path,
) -> None:
    manager = build_manager(fake_client, make_config, fake_clock, tmp_path)
    manager.evaluate_stuck_farm(active=True, build_message=stuck_message)
    receipt = manager.incidents[STUCK_FARM]["receipt"]

    fake_clock.advance(30)
    ack_time = fake_clock.now
    fake_client.receipt_info[receipt] = {
        "acknowledged": 1,
        "acknowledged_at": ack_time,
    }
    manager.evaluate_stuck_farm(active=True, build_message=stuck_message)
    assert len(fake_client.sent) == 1

    # Just under the 600 s ack timeout: no re-page.
    fake_clock.advance(599)
    manager.evaluate_stuck_farm(active=True, build_message=stuck_message)
    assert len(fake_client.sent) == 1

    # Crossing 600 s after the ack: one fresh emergency alert.
    fake_clock.advance(1)
    manager.evaluate_stuck_farm(active=True, build_message=stuck_message)
    assert len(fake_client.sent) == 2
    assert fake_client.sent[1]["priority"] == 2
    assert manager.incidents[STUCK_FARM]["receipt"] == "receipt-2"
    assert manager.incidents[STUCK_FARM]["acked_at"] == 0


def test_re_arm_disabled_when_re_alert_after_ack_is_zero(
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
    tmp_path: Path,
) -> None:
    manager = build_manager(
        fake_client, make_config, fake_clock, tmp_path, re_alert_after_ack=0
    )
    manager.evaluate_stuck_farm(active=True, build_message=stuck_message)
    receipt = manager.incidents[STUCK_FARM]["receipt"]

    fake_clock.advance(30)
    fake_client.receipt_info[receipt] = {
        "acknowledged": 1,
        "acknowledged_at": fake_clock.now,
    }
    manager.evaluate_stuck_farm(active=True, build_message=stuck_message)

    # Far beyond any sane ack timeout: still no re-page because it is disabled.
    fake_clock.advance(10_000)
    manager.evaluate_stuck_farm(active=True, build_message=stuck_message)
    assert len(fake_client.sent) == 1


def test_re_raise_fires_on_unacked_expiry(
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
    tmp_path: Path,
) -> None:
    manager = build_manager(fake_client, make_config, fake_clock, tmp_path)
    manager.evaluate_stuck_farm(active=True, build_message=stuck_message)
    receipt = manager.incidents[STUCK_FARM]["receipt"]
    opened_at = manager.incidents[STUCK_FARM]["opened_at"]
    fake_client.receipt_info[receipt] = {"acknowledged": 0}

    # One second before local expiry (opened_at + expire): no re-raise.
    fake_clock.advance(10800 - 1)
    manager.evaluate_stuck_farm(active=True, build_message=stuck_message)
    assert len(fake_client.sent) == 1

    # At local expiry, still un-acked: one fresh emergency alert, new receipt.
    fake_clock.advance(1)
    manager.evaluate_stuck_farm(active=True, build_message=stuck_message)
    assert len(fake_client.sent) == 2
    assert manager.incidents[STUCK_FARM]["receipt"] == "receipt-2"
    assert manager.incidents[STUCK_FARM]["opened_at"] == opened_at + 10800
    assert manager.incidents[STUCK_FARM]["acked_at"] == 0


def test_open_send_failure_does_not_record_incident(
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
    tmp_path: Path,
) -> None:
    class FailingClient(FakePushoverClient):
        def send(self, *args: object, **kwargs: object) -> str | None:
            return None  # emergency send failed

    failing = FailingClient()
    manager = IncidentManager(
        failing, make_config(), tmp_path / "state.json", clock=fake_clock
    )

    manager.evaluate_stuck_farm(active=True, build_message=stuck_message)

    # No receipt => no incident recorded; it retries on the next poll.
    assert STUCK_FARM not in manager.incidents


# -- auth_expired: emergency incident, independent of stuck_farm ---------------


def auth_message() -> str:
    return "refresh your Buckler cookies"


def test_auth_expired_opens_emergency_with_own_tag_and_is_independent(
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
    tmp_path: Path,
) -> None:
    manager = build_manager(fake_client, make_config, fake_clock, tmp_path)

    manager.evaluate_auth_expired(active=True, build_message=auth_message)
    assert AUTH_EXPIRED in manager.incidents
    sent = fake_client.sent[0]
    assert sent["priority"] == 2
    assert sent["retry"] == 120
    assert sent["expire"] == 10800
    assert sent["tags"] == AUTH_EXPIRED_TAG

    # stuck_farm is tracked separately; both can be OPEN at once.
    manager.evaluate_stuck_farm(active=True, build_message=stuck_message)
    assert STUCK_FARM in manager.incidents
    assert AUTH_EXPIRED in manager.incidents

    # Closing auth_expired cancels only its own receipt and leaves stuck_farm.
    auth_receipt = manager.incidents[AUTH_EXPIRED]["receipt"]
    manager.evaluate_auth_expired(active=False, build_message=auth_message)
    assert AUTH_EXPIRED not in manager.incidents
    assert STUCK_FARM in manager.incidents
    assert fake_client.cancelled == [auth_receipt]


# -- pending cancel retry -----------------------------------------------------


def test_failed_cancel_is_retried_next_poll(
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
    tmp_path: Path,
) -> None:
    manager = build_manager(fake_client, make_config, fake_clock, tmp_path)
    manager.evaluate_stuck_farm(active=True, build_message=stuck_message)
    receipt = manager.incidents[STUCK_FARM]["receipt"]

    fake_client.cancel_result = False
    manager.evaluate_stuck_farm(active=False, build_message=stuck_message)
    assert manager.pending_cancel == [receipt]
    assert STUCK_FARM not in manager.incidents

    fake_client.cancel_result = True
    manager.retry_pending_cancels()
    assert manager.pending_cancel == []


# -- api_down: one-shot high priority + courtesy recovery ---------------------


def test_api_down_sends_one_message_then_recovers_with_courtesy(
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
    tmp_path: Path,
) -> None:
    manager = build_manager(fake_client, make_config, fake_clock, tmp_path)

    manager.evaluate_api_down(active=True, down_message="Capcom Buckler website down?")
    assert len(fake_client.sent) == 1
    assert fake_client.sent[0]["priority"] == 1
    assert API_DOWN in manager.incidents

    # Still down: one-shot, no further messages.
    manager.evaluate_api_down(active=True, down_message="Capcom Buckler website down?")
    assert len(fake_client.sent) == 1

    fake_clock.advance(180)
    manager.evaluate_api_down(active=False)

    assert API_DOWN not in manager.incidents
    assert len(fake_client.sent) == 2
    assert fake_client.sent[1]["priority"] == 0
    assert "recovered after" in fake_client.sent[1]["message"]


def test_api_down_recovery_without_incident_is_a_noop(
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
    tmp_path: Path,
) -> None:
    manager = build_manager(fake_client, make_config, fake_clock, tmp_path)

    manager.evaluate_api_down(active=False)

    assert fake_client.sent == []
    assert API_DOWN not in manager.incidents


# -- startup reconciliation ---------------------------------------------------


def test_corrupt_state_file_triggers_cancel_by_tag_and_rebuild(
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "notification_state.json"
    state_path.write_text("{ this is not valid json", encoding="utf-8")

    manager = IncidentManager(fake_client, make_config(), state_path, clock=fake_clock)
    manager.reconcile_on_startup()

    # Every emergency incident type's dangling retries are cancelled by tag.
    assert fake_client.cancelled_tags == [STUCK_FARM_TAG, AUTH_EXPIRED_TAG]
    assert manager.incidents == {}
    rebuilt = json.loads(state_path.read_text(encoding="utf-8"))
    assert rebuilt["incidents"] == {}
    assert rebuilt["last_change_at"] == fake_clock.now


def test_missing_state_file_triggers_cancel_by_tag(
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
    tmp_path: Path,
) -> None:
    manager = IncidentManager(
        fake_client, make_config(), tmp_path / "missing.json", clock=fake_clock
    )
    manager.reconcile_on_startup()

    assert fake_client.cancelled_tags == [STUCK_FARM_TAG, AUTH_EXPIRED_TAG]


def test_valid_state_file_does_not_reconcile(
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "notification_state.json"
    state_path.write_text(
        json.dumps({"incidents": {}, "last_change_at": 500.0, "pending_cancel": []}),
        encoding="utf-8",
    )

    manager = IncidentManager(fake_client, make_config(), state_path, clock=fake_clock)
    manager.reconcile_on_startup()

    assert fake_client.cancelled_tags == []
    assert manager.last_change_at == 500.0


# -- persistence / crash recovery ---------------------------------------------


def test_open_incident_survives_reload(
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "notification_state.json"
    manager = IncidentManager(fake_client, make_config(), state_path, clock=fake_clock)
    manager.evaluate_stuck_farm(active=True, build_message=stuck_message)
    receipt = manager.incidents[STUCK_FARM]["receipt"]

    reloaded = IncidentManager(fake_client, make_config(), state_path, clock=fake_clock)
    assert reloaded.incidents[STUCK_FARM]["receipt"] == receipt


# -- pushover disabled: transitions still happen, no network ------------------


def test_disabled_pushover_still_transitions_without_network(
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
    tmp_path: Path,
) -> None:
    manager = build_manager(
        fake_client, make_config, fake_clock, tmp_path, pushover_enabled=False
    )

    manager.evaluate_stuck_farm(active=True, build_message=stuck_message)
    assert STUCK_FARM in manager.incidents
    assert manager.incidents[STUCK_FARM]["receipt"] is None
    assert fake_client.sent == []

    # Stale polls do nothing (no receipt to inspect, no network).
    fake_clock.advance(60)
    manager.evaluate_stuck_farm(active=True, build_message=stuck_message)
    assert fake_client.sent == []

    # Recovery closes the incident without a cancel call.
    manager.evaluate_stuck_farm(active=False, build_message=stuck_message)
    assert STUCK_FARM not in manager.incidents
    assert fake_client.cancelled == []


# -- last_change_at stuck timer (replaces M10 mtime check) --------------------


def test_record_change_resets_seconds_since_last_change(
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
    tmp_path: Path,
) -> None:
    manager = build_manager(fake_client, make_config, fake_clock, tmp_path)

    fake_clock.advance(300)
    assert manager.seconds_since_last_change() == 300

    manager.record_change()
    assert manager.seconds_since_last_change() == 0
    assert manager.last_change_at == fake_clock.now
