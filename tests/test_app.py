"""Tests for the monitor's startup account check (app.py)."""

import logging
from collections.abc import Callable

import pytest
import requests

import app
from api_service import AuthExpiredError
from config import ConfigData


def test_matching_account_lets_the_monitor_start(
    monkeypatch: pytest.MonkeyPatch, make_config: Callable[..., ConfigData]
) -> None:
    config = make_config(user_code=1234567890)
    monkeypatch.setattr(app, "get_logged_in_short_id", lambda config: 1234567890)

    app.check_monitored_account(config)


def test_another_accounts_cookies_stop_the_monitor(
    monkeypatch: pytest.MonkeyPatch,
    make_config: Callable[..., ConfigData],
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = make_config(user_code=1234567890)
    monkeypatch.setattr(app, "get_logged_in_short_id", lambda config: 2222222222)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(SystemExit) as exit_info:
            app.check_monitored_account(config)

    assert exit_info.value.code == 1
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(errors) == 1
    # The message names both accounts and the way out.
    assert "2222222222" in errors[0].getMessage()
    assert "1234567890" in errors[0].getMessage()
    assert "login.py" in errors[0].getMessage()


@pytest.mark.parametrize(
    "exception",
    [
        AuthExpiredError("Buckler reports no logged-in account"),
        requests.ConnectionError("no route to host"),
        ValueError("unexpected login data"),
    ],
)
def test_a_check_that_cannot_complete_does_not_stop_the_monitor(
    monkeypatch: pytest.MonkeyPatch,
    make_config: Callable[..., ConfigData],
    caplog: pytest.LogCaptureFixture,
    exception: Exception,
) -> None:
    def raise_exception(config: ConfigData) -> int:
        raise exception

    monkeypatch.setattr(app, "get_logged_in_short_id", raise_exception)

    with caplog.at_level(logging.DEBUG):
        # Expired cookies and outages are the first poll's to report.
        app.check_monitored_account(make_config())

    assert [record.levelno for record in caplog.records] == [logging.WARNING]
    assert type(exception).__name__ in caplog.records[0].getMessage()
