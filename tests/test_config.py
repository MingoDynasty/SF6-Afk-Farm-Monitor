"""Config validation: pydantic field constraints reject nonsensical values
(review finding L4)."""

from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from config import ConfigData


def test_valid_config_constructs(make_config: Callable[..., ConfigData]) -> None:
    config = make_config()
    assert config.polling_interval == 60
    assert config.emergency_expire == 10800


@pytest.mark.parametrize(
    "overrides",
    [
        {"polling_interval": 0},
        {"polling_interval": -1},
        {"battle_count_timeout": 0},
        {"emergency_retry": 29},  # below the Pushover minimum of 30
        {"emergency_expire": 0},
        {"emergency_expire": 10801},  # above the Pushover maximum of 10800
        {"re_alert_after_ack": -1},
    ],
)
def test_out_of_range_values_are_rejected(
    make_config: Callable[..., ConfigData], overrides: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        make_config(**overrides)


def test_re_alert_after_ack_zero_is_allowed(
    make_config: Callable[..., ConfigData],
) -> None:
    # 0 disables the re-arm timer, so it must stay valid (ge=0, not gt=0).
    assert make_config(re_alert_after_ack=0).re_alert_after_ack == 0
