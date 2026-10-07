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


# -- Master Pass points and the logged-in account ------------------------------


def patch_get(
    monkeypatch: pytest.MonkeyPatch,
    response: FakeResponse,
    captured_request: dict[str, Any] | None = None,
) -> None:
    def fake_request(
        method: str, url: str, headers: dict[str, str], timeout: tuple[int, int]
    ) -> FakeResponse:
        if captured_request is not None:
            captured_request.update(
                {"method": method, "url": url, "headers": headers, "timeout": timeout}
            )
        return response

    monkeypatch.setattr(api_service.requests, "request", fake_request)


def master_pass(season_id: int, points: dict[int, int] | None) -> dict[str, Any]:
    """One season's pass in the shape Buckler returns, extra fields included."""
    characters = None
    if points is not None:
        characters = [
            {
                "character_id": character_id,
                "sort": index,
                "point": point,
                "tier_list": [
                    {
                        "tier_no": 1,
                        "tier_point": 100,
                        "is_received": point >= 100,
                        "item_list": [
                            {"item_category": 6, "item_id": "x.png", "num": 1}
                        ],
                    }
                ],
            }
            for index, (character_id, point) in enumerate(points.items(), start=1)
        ]
    return {
        "season_id": season_id,
        "start_at": 1785567600,
        "end_at": 1793516399,
        "characters": characters,
    }


def master_pass_response(*passes: dict[str, Any]) -> FakeResponse:
    body = {"messageList": {"master_rate_pass_list": list(passes)}}
    return FakeResponse(status_code=200, json_data=body, text=json.dumps(body))


def login_data_response(short_id: int, logged_in: bool) -> FakeResponse:
    body = {
        "loginUser": {
            "platformId": 3 if logged_in else 0,
            "shortId": short_id,
            "fighterId": "Fighter" if logged_in else "",
            "flg": logged_in,
            "regionId": 1 if logged_in else 0,
        }
    }
    return FakeResponse(status_code=200, json_data=body, text=json.dumps(body))


def test_get_master_pass_points_reads_the_configured_season(
    monkeypatch: pytest.MonkeyPatch, config_data: ConfigData
) -> None:
    captured_request: dict[str, Any] = {}
    patch_get(
        monkeypatch,
        master_pass_response(
            master_pass(11, {2: 7}),
            master_pass(config_data.target_season_id, {253: 201, 2: 101, 21: 0}),
        ),
        captured_request,
    )

    points = api_service.get_master_pass_points(config_data)

    assert points == {253: 201, 2: 101, 21: 0}
    assert captured_request["method"] == "GET"
    assert captured_request["url"] == api_service.MASTER_PASS_URL
    assert captured_request["headers"]["Cookie"] == (
        "buckler_id=buckler-id; buckler_r_id=buckler-r-id; "
        "buckler_praise_date=1234567890123"
    )
    assert captured_request["timeout"] == api_service.REQUEST_TIMEOUT


@pytest.mark.parametrize(
    "passes",
    [
        # The season rolled over and config.toml still names the old one.
        [master_pass(13, {2: 5})],
        # The configured season is listed but its pass is closed.
        [master_pass(12, None)],
        [],
    ],
)
def test_master_pass_without_the_configured_season_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
    config_data: ConfigData,
    caplog: pytest.LogCaptureFixture,
    passes: list[dict[str, Any]],
) -> None:
    response = master_pass_response(*passes)
    patch_get(monkeypatch, response)

    with caplog.at_level(logging.DEBUG):
        # Returning no points here would read as "nobody has finished" forever.
        with pytest.raises(ValueError, match="season 12") as error:
            api_service.get_master_pass_points(config_data)

    assert "target_season_id" in str(error.value)
    assert response.text in caplog.text


def test_get_logged_in_short_id_returns_the_account(
    monkeypatch: pytest.MonkeyPatch, config_data: ConfigData
) -> None:
    captured_request: dict[str, Any] = {}
    patch_get(
        monkeypatch, login_data_response(1234567890, logged_in=True), captured_request
    )

    assert api_service.get_logged_in_short_id(config_data) == 1234567890
    assert captured_request["method"] == "GET"
    assert captured_request["url"] == api_service.LOGIN_DATA_URL


def test_logged_out_login_data_classified_as_auth_expired(
    monkeypatch: pytest.MonkeyPatch,
    config_data: ConfigData,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Logged out, Buckler still answers 200, with a zero short ID.
    response = login_data_response(0, logged_in=False)
    patch_get(monkeypatch, response)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(api_service.AuthExpiredError):
            api_service.get_logged_in_short_id(config_data)

    assert response.text in caplog.text


ACCOUNT_FETCHERS = [
    api_service.get_master_pass_points,
    api_service.get_logged_in_short_id,
]


@pytest.mark.parametrize("fetch", ACCOUNT_FETCHERS)
def test_account_endpoints_classify_http_403_as_auth_expired(
    monkeypatch: pytest.MonkeyPatch,
    config_data: ConfigData,
    caplog: pytest.LogCaptureFixture,
    fetch: Any,
) -> None:
    patch_get(
        monkeypatch, FakeResponse(status_code=403, text='{"message": "not logged in"}')
    )

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(api_service.AuthExpiredError):
            fetch(config_data)

    assert '{"message": "not logged in"}' in caplog.text


@pytest.mark.parametrize("fetch", ACCOUNT_FETCHERS)
def test_account_endpoints_classify_http_500_as_outage(
    monkeypatch: pytest.MonkeyPatch, config_data: ConfigData, fetch: Any
) -> None:
    patch_get(monkeypatch, FakeResponse(status_code=500, text="server error"))

    with pytest.raises(HTTPError):
        fetch(config_data)


@pytest.mark.parametrize("fetch", ACCOUNT_FETCHERS)
def test_account_endpoints_classify_html_200_as_auth_expired(
    monkeypatch: pytest.MonkeyPatch, config_data: ConfigData, fetch: Any
) -> None:
    patch_get(
        monkeypatch,
        FakeResponse(status_code=200, text="<html>log in</html>", json_error=True),
    )

    with pytest.raises(api_service.AuthExpiredError):
        fetch(config_data)


@pytest.mark.parametrize("fetch", ACCOUNT_FETCHERS)
def test_account_endpoints_reraise_schema_drift_and_log_body(
    monkeypatch: pytest.MonkeyPatch,
    config_data: ConfigData,
    caplog: pytest.LogCaptureFixture,
    fetch: Any,
) -> None:
    body_text = '{"renamed": {}}'
    patch_get(
        monkeypatch,
        FakeResponse(status_code=200, json_data={"renamed": {}}, text=body_text),
    )

    with caplog.at_level(logging.DEBUG):
        # A changed shape must not be mistaken for expired cookies.
        with pytest.raises(ValidationError):
            fetch(config_data)

    assert body_text in caplog.text
