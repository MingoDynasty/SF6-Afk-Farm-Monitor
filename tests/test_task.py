import json
import os
from collections.abc import Callable, Generator
from pathlib import Path

import pytest

import task
from config import ConfigData
from conftest import FakeClock, FakePushoverClient
from incident_manager import STUCK_FARM, IncidentManager
from model import CharacterWinRate, WinRateResponse


@pytest.fixture(autouse=True)
def clear_notification_queue() -> Generator[None]:
    task.notifications_to_send.clear()
    yield
    task.notifications_to_send.clear()


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
    sent_messages: list[str],
) -> None:
    def fake_get_character_win_rates(config: ConfigData) -> WinRateResponse:
        return win_rate_response

    def fake_send_message(message: str, config: ConfigData) -> None:
        sent_messages.append(message)

    monkeypatch.setattr(task, "get_character_win_rates", fake_get_character_win_rates)
    monkeypatch.setattr(task, "send_message", fake_send_message)

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
    sent_messages: list[str] = []

    run_task_with_response(
        monkeypatch,
        config_data,
        manager,
        database_path,
        make_response({"Ryu": 10, "Akuma": 1}),
        sent_messages,
    )

    assert read_database(database_path) == {"Akuma": 1, "Ryu": 10}
    assert sent_messages == []
    assert fake_client.sent == []


def test_threshold_crossing_sends_master_color_notification(
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
    sent_messages: list[str] = []

    run_task_with_response(
        monkeypatch,
        config_data,
        manager,
        database_path,
        make_response({"Juri": 100}),
        sent_messages,
    )

    assert read_database(database_path) == {"Juri": 100}
    assert sent_messages == ["Finished Master color reward for character: Juri"]
    # Master-color messages still go through send_message, not the incident
    # manager (conversion is phase 2).
    assert fake_client.sent == []


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
    sent_messages: list[str] = []

    run_task_with_response(
        monkeypatch,
        config_data,
        manager,
        database_path,
        make_response({"Manon": 42}),
        sent_messages,
    )

    assert read_database(database_path) == {"Manon": 42}
    # Stuck farm is an emergency incident now, not a plain send_message.
    assert sent_messages == []
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
    sent_messages: list[str] = []

    run_task_with_response(
        monkeypatch,
        config_data,
        manager,
        database_path,
        make_response({"Manon": 43}),
        sent_messages,
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
    sent_messages: list[str] = []

    run_task_with_response(
        monkeypatch,
        config_data,
        manager,
        database_path,
        make_response({"Chun-Li": 7}),
        sent_messages,
    )

    assert read_database(database_path) == {"Chun-Li": 7}
    assert sent_messages == []
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
