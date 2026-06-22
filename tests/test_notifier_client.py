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
