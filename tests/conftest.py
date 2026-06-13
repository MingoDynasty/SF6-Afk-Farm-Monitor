"""Shared test fixtures: a fake clock and a fake Pushover client.

The fake client subclasses :class:`PushoverClient` so it type-checks where a
real client is expected, but overrides every network method — no real Pushover
messages are ever sent from the test suite.
"""

from collections.abc import Callable
from typing import Any

import pytest

from config import ConfigData
from notifier_client import PushoverClient


class FakeClock:
    """A monotonically advancing clock under the test's control."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakePushoverClient(PushoverClient):
    """Records calls and returns programmable results without any network I/O."""

    def __init__(self) -> None:
        super().__init__("fake-app-key", "fake-user-key")
        self.sent: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.cancelled_tags: list[str] = []
        self.checked: list[str] = []
        # receipt -> dict returned by check_receipt (empty/missing => failure)
        self.receipt_info: dict[str, dict[str, Any]] = {}
        self.cancel_result = True
        self._receipt_counter = 0

    def send(
        self,
        message: str,
        priority: int = 0,
        retry: int | None = None,
        expire: int | None = None,
        tags: str | None = None,
        sound: str | None = None,
        url: str | None = None,
        url_title: str | None = None,
        timestamp: int | None = None,
    ) -> str | None:
        self.sent.append(
            {
                "message": message,
                "priority": priority,
                "retry": retry,
                "expire": expire,
                "tags": tags,
                "sound": sound,
                "url": url,
                "url_title": url_title,
                "timestamp": timestamp,
            }
        )
        # Mirror the real client: only emergency (priority=2) sends return a
        # receipt; priority<2 returns None even on success.
        if priority == 2:
            self._receipt_counter += 1
            return f"receipt-{self._receipt_counter}"
        return None

    def check_receipt(self, receipt: str) -> dict[str, Any]:
        self.checked.append(receipt)
        return dict(self.receipt_info.get(receipt, {}))

    def cancel(self, receipt: str) -> bool:
        self.cancelled.append(receipt)
        return self.cancel_result

    def cancel_by_tag(self, tag: str) -> bool:
        self.cancelled_tags.append(tag)
        return True


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def fake_client() -> FakePushoverClient:
    return FakePushoverClient()


@pytest.fixture
def make_config() -> Callable[..., ConfigData]:
    def _make(**overrides: Any) -> ConfigData:
        defaults: dict[str, Any] = {
            "user_code": 1234567890,
            "target_season_id": 12,
            "polling_interval": 60,
            "battle_count_timeout": 60,
            "buckler_id": "buckler-id",
            "buckler_r_id": "buckler-r-id",
            "buckler_praise_date": 1234567890123,
            "pushover_enabled": True,
            "pushover_app_key": "app-key",
            "pushover_user_key": "user-key",
            "emergency_retry": 120,
            "emergency_expire": 10800,
            "re_alert_after_ack": 600,
        }
        defaults.update(overrides)
        return ConfigData(**defaults)

    return _make
