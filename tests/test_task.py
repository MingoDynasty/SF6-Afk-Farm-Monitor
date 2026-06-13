import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest
import requests
from requests import HTTPError

import task
from api_service import AuthExpiredError
from config import ConfigData
from conftest import FakeClock, FakePushoverClient
from incident_manager import (
    API_DOWN,
    AUTH_EXPIRED,
    AUTH_EXPIRED_TAG,
    LOW_QUOTA,
    STUCK_FARM,
    SWAP_NEEDED,
    SWAP_NEEDED_TAG,
    IncidentManager,
)
from model import CharacterWinRate, WinRateResponse


def make_response(character_counts: dict[str, int]) -> WinRateResponse:
    return WinRateResponse(
        character_win_rates=[
            CharacterWinRate(
                battle_count=battle_count,
                character_id=index,
                win_count=0,
                character_name=character_name,
                character_alpha=character_name.lower(),
                character_tool_name=character_name.lower(),
                character_sort=index,
            )
            for index, (character_name, battle_count) in enumerate(
                character_counts.items(), start=1
            )
        ]
    )


def write_database(database_path: Path, data: dict[str, int]) -> None:
    database_path.write_text(json.dumps(data), encoding="utf-8")


def read_database(database_path: Path) -> dict[str, int]:
    return json.loads(database_path.read_text(encoding="utf-8"))


def run_task_with_response(
    monkeypatch: pytest.MonkeyPatch,
    config_data: ConfigData,
    incident_manager: IncidentManager,
    database_path: Path,
    win_rate_response: WinRateResponse,
) -> None:
    def fake_get_character_win_rates(config: ConfigData) -> WinRateResponse:
        return win_rate_response

    monkeypatch.setattr(task, "get_character_win_rates", fake_get_character_win_rates)

    task.do_task(config_data, incident_manager, database_path)


def build_manager(
    fake_client: FakePushoverClient,
    config: ConfigData,
    fake_clock: FakeClock,
    tmp_path: Path,
) -> IncidentManager:
    state_path = tmp_path / "notification_state.json"
    return IncidentManager(fake_client, config, state_path, clock=fake_clock)


def test_new_character_counts_as_difference_and_is_persisted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
) -> None:
    monkeypatch.chdir(tmp_path)
    config_data = make_config()
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)
    database_path = tmp_path / "database.json"
    write_database(database_path, {"Ryu": 10})

    run_task_with_response(
        monkeypatch,
        config_data,
        manager,
        database_path,
        make_response({"Ryu": 10, "Akuma": 1}),
    )

    assert read_database(database_path) == {"Akuma": 1, "Ryu": 10}
    assert fake_client.sent == []


def test_threshold_crossing_opens_swap_needed_incident(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
) -> None:
    monkeypatch.chdir(tmp_path)
    config_data = make_config()
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)
    database_path = tmp_path / "database.json"
    write_database(database_path, {"Juri": 99})

    run_task_with_response(
        monkeypatch,
        config_data,
        manager,
        database_path,
        make_response({"Juri": 100}),
    )

    assert read_database(database_path) == {"Juri": 100}
    # The 99 -> 100 crossing now opens a swap_needed emergency incident
    # (replaces the ffb650b per-match re-fire via send_message).
    assert SWAP_NEEDED in manager.incidents
    assert manager.incidents[SWAP_NEEDED]["character"] == "Juri"
    assert len(fake_client.sent) == 1
    assert fake_client.sent[0]["priority"] == 2
    assert fake_client.sent[0]["tags"] == SWAP_NEEDED_TAG
    assert "Juri" in fake_client.sent[0]["message"]


def test_swap_needed_stays_open_and_silent_while_same_character_gains(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
) -> None:
    monkeypatch.chdir(tmp_path)
    config_data = make_config()
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)
    database_path = tmp_path / "database.json"
    write_database(database_path, {"Juri": 99})

    # Crossing opens the incident.
    run_task_with_response(
        monkeypatch, config_data, manager, database_path, make_response({"Juri": 100})
    )
    receipt = manager.incidents[SWAP_NEEDED]["receipt"]
    fake_client.receipt_info[receipt] = {"acknowledged": 0}

    # Continued matches on the *same* finished character: OPEN and silent.
    for count in (101, 102, 103):
        fake_clock.advance(60)
        run_task_with_response(
            monkeypatch,
            config_data,
            manager,
            database_path,
            make_response({"Juri": count}),
        )

    assert SWAP_NEEDED in manager.incidents
    assert len(fake_client.sent) == 1


def test_swap_needed_closes_when_different_character_increases(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
) -> None:
    monkeypatch.chdir(tmp_path)
    config_data = make_config()
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)
    database_path = tmp_path / "database.json"
    write_database(database_path, {"Juri": 99, "Cammy": 5})

    # Juri crosses 100 -> incident opens (finished character = Juri).
    run_task_with_response(
        monkeypatch,
        config_data,
        manager,
        database_path,
        make_response({"Juri": 100, "Cammy": 5}),
    )
    receipt = manager.incidents[SWAP_NEEDED]["receipt"]
    assert SWAP_NEEDED in manager.incidents

    # The user swaps: a *different* character (Cammy) starts gaining -> CLOSED,
    # receipt cancelled. (Juri also still gaining the same poll must not block
    # the close.)
    fake_clock.advance(60)
    run_task_with_response(
        monkeypatch,
        config_data,
        manager,
        database_path,
        make_response({"Juri": 101, "Cammy": 6}),
    )

    assert SWAP_NEEDED not in manager.incidents
    assert fake_client.cancelled == [receipt]


def test_stuck_detection_opens_emergency_incident(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
) -> None:
    monkeypatch.chdir(tmp_path)
    config_data = make_config(battle_count_timeout=60)
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)
    database_path = tmp_path / "database.json"
    write_database(database_path, {"Manon": 42})

    # No count change, and last_change_at is now older than the timeout.
    fake_clock.advance(120)

    run_task_with_response(
        monkeypatch,
        config_data,
        manager,
        database_path,
        make_response({"Manon": 42}),
    )

    assert read_database(database_path) == {"Manon": 42}
    # Stuck farm is an emergency incident.
    assert len(fake_client.sent) == 1
    assert fake_client.sent[0]["priority"] == 2
    assert "afk farm might be stuck" in fake_client.sent[0]["message"]
    assert STUCK_FARM in manager.incidents


def test_count_change_resets_stuck_timer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
) -> None:
    monkeypatch.chdir(tmp_path)
    config_data = make_config(battle_count_timeout=60)
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)
    database_path = tmp_path / "database.json"
    write_database(database_path, {"Manon": 42})

    # Plenty of elapsed time, but the count changes this poll, so the timer
    # resets and no stuck incident opens.
    fake_clock.advance(120)

    run_task_with_response(
        monkeypatch,
        config_data,
        manager,
        database_path,
        make_response({"Manon": 43}),
    )

    assert read_database(database_path) == {"Manon": 43}
    assert fake_client.sent == []
    assert STUCK_FARM not in manager.incidents


def test_corrupt_database_is_replaced_from_current_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
) -> None:
    monkeypatch.chdir(tmp_path)
    config_data = make_config()
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)
    database_path = tmp_path / "database.json"
    database_path.write_text("{", encoding="utf-8")

    run_task_with_response(
        monkeypatch,
        config_data,
        manager,
        database_path,
        make_response({"Chun-Li": 7}),
    )

    assert read_database(database_path) == {"Chun-Li": 7}
    assert fake_client.sent == []


def test_api_failure_opens_api_down_incident(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
) -> None:
    monkeypatch.chdir(tmp_path)
    config_data = make_config()
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)
    database_path = tmp_path / "database.json"

    def fake_get_character_win_rates(config: ConfigData) -> WinRateResponse:
        raise RuntimeError("boom")

    monkeypatch.setattr(task, "get_character_win_rates", fake_get_character_win_rates)

    task.do_task(config_data, manager, database_path)

    assert len(fake_client.sent) == 1
    assert fake_client.sent[0]["priority"] == 1


@pytest.mark.parametrize(
    "exception",
    [
        HTTPError("HTTP 500"),
        requests.ConnectionError("no route to host"),
        requests.Timeout("read timed out"),
    ],
)
def test_outage_errors_open_api_down_not_auth_expired(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
    exception: Exception,
) -> None:
    monkeypatch.chdir(tmp_path)
    config_data = make_config()
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)
    database_path = tmp_path / "database.json"

    def fake_get_character_win_rates(config: ConfigData) -> WinRateResponse:
        raise exception

    monkeypatch.setattr(task, "get_character_win_rates", fake_get_character_win_rates)

    task.do_task(config_data, manager, database_path)

    assert API_DOWN in manager.incidents
    assert AUTH_EXPIRED not in manager.incidents
    assert len(fake_client.sent) == 1
    assert fake_client.sent[0]["priority"] == 1


def test_auth_expired_opens_emergency_incident(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
) -> None:
    monkeypatch.chdir(tmp_path)
    config_data = make_config()
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)
    database_path = tmp_path / "database.json"

    def fake_get_character_win_rates(config: ConfigData) -> WinRateResponse:
        raise AuthExpiredError("cookies expired")

    monkeypatch.setattr(task, "get_character_win_rates", fake_get_character_win_rates)

    task.do_task(config_data, manager, database_path)

    # Emergency incident (priority 2) with its own tag, not an api_down.
    assert AUTH_EXPIRED in manager.incidents
    assert API_DOWN not in manager.incidents
    assert len(fake_client.sent) == 1
    assert fake_client.sent[0]["priority"] == 2
    assert fake_client.sent[0]["tags"] == AUTH_EXPIRED_TAG


def test_successful_poll_closes_open_auth_expired_incident(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
) -> None:
    monkeypatch.chdir(tmp_path)
    config_data = make_config()
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)
    database_path = tmp_path / "database.json"
    write_database(database_path, {"Ryu": 1})

    # An auth_expired incident is already open from a previous failed poll.
    manager.evaluate_auth_expired(active=True, build_message=lambda: "refresh cookies")
    receipt = manager.incidents[AUTH_EXPIRED]["receipt"]

    run_task_with_response(
        monkeypatch,
        config_data,
        manager,
        database_path,
        make_response({"Ryu": 1}),
    )

    # First successful poll closes the incident and cancels the receipt.
    assert AUTH_EXPIRED not in manager.incidents
    assert fake_client.cancelled == [receipt]


def test_do_task_opens_low_quota_incident_when_remaining_below_floor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
) -> None:
    monkeypatch.chdir(tmp_path)
    config_data = make_config()
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)
    database_path = tmp_path / "database.json"
    write_database(database_path, {"Ryu": 1})

    # A prior Pushover call this session reported a low remaining count.
    fake_client.last_remaining = 400

    run_task_with_response(
        monkeypatch,
        config_data,
        manager,
        database_path,
        make_response({"Ryu": 1}),
    )

    assert LOW_QUOTA in manager.incidents


def test_write_to_database_uses_atomic_replace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    database_path = tmp_path / "database.json"
    real_replace = os.replace
    replace_calls: list[tuple[Path, Path]] = []

    def record_replace(source: str | Path, destination: str | Path) -> None:
        replace_calls.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(task.os, "replace", record_replace)

    task.write_to_database({"Ryu": 1}, database_path)

    temporary_database_path = database_path.with_name("database.json.tmp")
    assert replace_calls == [(temporary_database_path, database_path)]
    assert not temporary_database_path.exists()
    assert read_database(database_path) == {"Ryu": 1}
