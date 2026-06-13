import json
from pathlib import Path
from typing import Any

import pytest

import api_service
from config import ConfigData


@pytest.fixture
def config_data() -> ConfigData:
    return ConfigData(
        user_code=1234567890,
        target_season_id=12,
        polling_interval=60,
        battle_count_timeout=60,
        buckler_id="buckler-id",
        buckler_r_id="buckler-r-id",
        buckler_praise_date=1234567890123,
        pushover_enabled=False,
        pushover_app_key="app-key",
        pushover_user_key="user-key",
    )


def test_get_character_win_rates_builds_payload_from_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config_data: ConfigData
) -> None:
    monkeypatch.chdir(tmp_path)
    captured_request: dict[str, Any] = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"response": {"character_win_rates": []}}

    def fake_request(
        method: str,
        url: str,
        headers: dict[str, str],
        data: str,
        timeout: tuple[int, int],
    ) -> FakeResponse:
        captured_request.update(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "data": data,
                "timeout": timeout,
            }
        )
        return FakeResponse()

    monkeypatch.setattr(api_service.requests, "request", fake_request)

    response = api_service.get_character_win_rates(config_data)

    payload = json.loads(captured_request["data"])
    assert payload["targetShortId"] == config_data.user_code
    assert payload["targetSeasonId"] == config_data.target_season_id
    assert (
        captured_request["headers"]["Referer"]
        == f"https://www.streetfighter.com/6/buckler/profile/{config_data.user_code}/play"
    )
    assert captured_request["timeout"] == api_service.REQUEST_TIMEOUT
    assert response.character_win_rates == []
