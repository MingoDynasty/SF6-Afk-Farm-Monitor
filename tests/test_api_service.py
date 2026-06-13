import json
import logging
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from requests import HTTPError

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


# -- M3: auth-expiry vs outage classification + M9: failure-path body logging --


class FakeResponse:
    """A minimal stand-in for a requests.Response."""

    def __init__(
        self,
        status_code: int = 200,
        json_data: Any = None,
        text: str = "",
        json_error: bool = False,
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data
        self.text = text
        self._json_error = json_error

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise HTTPError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        if self._json_error:
            raise ValueError("No JSON object could be decoded")
        return self._json_data


def patch_response(monkeypatch: pytest.MonkeyPatch, response: FakeResponse) -> None:
    def fake_request(
        method: str,
        url: str,
        headers: dict[str, str],
        data: str,
        timeout: tuple[int, int],
    ) -> FakeResponse:
        return response

    monkeypatch.setattr(api_service.requests, "request", fake_request)


@pytest.mark.parametrize("status_code", [401, 403])
def test_http_401_403_classified_as_auth_expired_and_logs_body(
    monkeypatch: pytest.MonkeyPatch,
    config_data: ConfigData,
    caplog: pytest.LogCaptureFixture,
    status_code: int,
) -> None:
    patch_response(
        monkeypatch,
        FakeResponse(status_code=status_code, text="<html>login</html>"),
    )

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(api_service.AuthExpiredError):
            api_service.get_character_win_rates(config_data)

    assert "<html>login</html>" in caplog.text


def test_html_200_body_classified_as_auth_expired_and_logs_body(
    monkeypatch: pytest.MonkeyPatch,
    config_data: ConfigData,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # HTTP 200 but the body is an HTML login page, not JSON.
    patch_response(
        monkeypatch,
        FakeResponse(
            status_code=200, text="<html>please log in</html>", json_error=True
        ),
    )

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(api_service.AuthExpiredError):
            api_service.get_character_win_rates(config_data)

    assert "<html>please log in</html>" in caplog.text


def test_missing_response_key_classified_as_auth_expired_and_logs_body(
    monkeypatch: pytest.MonkeyPatch,
    config_data: ConfigData,
    caplog: pytest.LogCaptureFixture,
) -> None:
    body_text = '{"unexpected": 1}'
    patch_response(
        monkeypatch,
        FakeResponse(status_code=200, json_data={"unexpected": 1}, text=body_text),
    )

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(api_service.AuthExpiredError):
            api_service.get_character_win_rates(config_data)

    assert body_text in caplog.text


def test_http_500_classified_as_outage_and_logs_body(
    monkeypatch: pytest.MonkeyPatch,
    config_data: ConfigData,
    caplog: pytest.LogCaptureFixture,
) -> None:
    patch_response(
        monkeypatch,
        FakeResponse(status_code=500, text="internal server error"),
    )

    with caplog.at_level(logging.DEBUG):
        # A 5xx is an outage (HTTPError), NOT an auth-expiry classification.
        with pytest.raises(HTTPError):
            api_service.get_character_win_rates(config_data)

    assert "internal server error" in caplog.text


def test_validation_error_logs_body_and_reraises(
    monkeypatch: pytest.MonkeyPatch,
    config_data: ConfigData,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # "response" present but the wrong shape -> pydantic ValidationError.
    body_text = '{"response": {"character_win_rates": "not-a-list"}}'
    patch_response(
        monkeypatch,
        FakeResponse(
            status_code=200,
            json_data={"response": {"character_win_rates": "not-a-list"}},
            text=body_text,
        ),
    )

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ValidationError):
            api_service.get_character_win_rates(config_data)

    assert body_text in caplog.text


def test_successful_poll_does_not_log_response_body(
    monkeypatch: pytest.MonkeyPatch,
    config_data: ConfigData,
    caplog: pytest.LogCaptureFixture,
) -> None:
    body_text = '{"response": {"character_win_rates": []}}'
    patch_response(
        monkeypatch,
        FakeResponse(
            status_code=200,
            json_data={"response": {"character_win_rates": []}},
            text=body_text,
        ),
    )

    with caplog.at_level(logging.DEBUG):
        response = api_service.get_character_win_rates(config_data)

    assert response.character_win_rates == []
    # The body is logged ONLY on failure paths (M9).
    assert body_text not in caplog.text
