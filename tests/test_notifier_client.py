"""Unit tests for PushoverClient.cancel transient-vs-permanent handling.

cancel() feeds IncidentManager.pending_cancel, which retries on a False return.
A False must mean "transient, retry"; a permanent 4xx (e.g. the 404 Pushover
returns for an expired receipt) must settle to True so a stale receipt is not
retried forever.
"""

from typing import Any

import pytest
import requests

from notifier_client import PushoverClient


class FakeResponse:
    """Minimal stand-in for a requests.Response."""

    def __init__(
        self,
        status_code: int = 200,
        json_data: Any = None,
        json_error: bool = False,
    ) -> None:
        self.status_code = status_code
        self._json_data = json_data
        self._json_error = json_error
        self.headers: dict[str, str] = {}

    def json(self) -> Any:
        if self._json_error:
            raise ValueError("No JSON object could be decoded")
        return self._json_data


def patch_post(monkeypatch: pytest.MonkeyPatch, response: FakeResponse) -> None:
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: response)


def patch_post_sequence(
    monkeypatch: pytest.MonkeyPatch, responses: list[FakeResponse]
) -> None:
    response_iter = iter(responses)
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: next(response_iter))


@pytest.fixture
def client() -> PushoverClient:
    return PushoverClient("app-key", "user-key")


def test_cancel_success_returns_true(
    monkeypatch: pytest.MonkeyPatch, client: PushoverClient
) -> None:
    patch_post(monkeypatch, FakeResponse(200, {"status": 1}))
    assert client.cancel("receipt-abc") is True


def test_cancel_404_settles_to_true(
    monkeypatch: pytest.MonkeyPatch,
    client: PushoverClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Expired/invalid receipt: nothing left to cancel, so the cancel is settled
    # (dropped from pending_cancel) rather than retried forever.
    patch_post(
        monkeypatch,
        FakeResponse(
            404,
            {"status": 0, "errors": ["receipt not found; may be invalid or expired"]},
        ),
    )
    with caplog.at_level("INFO"):
        assert client.cancel("rwx91s3dnkwsg517eoroqctfura4ug") is True
    # Logged once at INFO, not as a repeating ERROR.
    assert "receipt already gone" in caplog.text
    assert "failed" not in caplog.text


def test_cancel_429_is_transient(
    monkeypatch: pytest.MonkeyPatch,
    client: PushoverClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    patch_post(monkeypatch, FakeResponse(429, {"status": 0}))

    with caplog.at_level("WARNING"):
        assert client.cancel("receipt-abc") is False

    rate_limit_logs = [
        record for record in caplog.records if "rate-limited" in record.message
    ]
    assert len(rate_limit_logs) == 1
    assert rate_limit_logs[0].levelname == "WARNING"
    assert "receipt already gone" not in caplog.text


def test_cancel_429_then_404_retries_then_settles(
    monkeypatch: pytest.MonkeyPatch,
    client: PushoverClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    patch_post_sequence(
        monkeypatch,
        [
            FakeResponse(429, {"status": 0}),
            FakeResponse(
                404,
                {
                    "status": 0,
                    "errors": ["receipt not found; may be invalid or expired"],
                },
            ),
        ],
    )

    with caplog.at_level("INFO"):
        assert client.cancel("receipt-abc") is False
        assert client.cancel("receipt-abc") is True

    assert "rate-limited" in caplog.text
    assert "receipt already gone" in caplog.text


def test_cancel_network_error_is_transient(
    monkeypatch: pytest.MonkeyPatch, client: PushoverClient
) -> None:
    def boom(*args: Any, **kwargs: Any) -> FakeResponse:
        raise requests.ConnectionError("network down")

    monkeypatch.setattr(requests, "post", boom)
    assert client.cancel("receipt-abc") is False


def test_cancel_server_error_is_transient(
    monkeypatch: pytest.MonkeyPatch, client: PushoverClient
) -> None:
    patch_post(monkeypatch, FakeResponse(500, {"status": 0}))
    assert client.cancel("receipt-abc") is False
