"""Cookie extraction and verification logic for login.py.

The interactive browser capture (`capture_cookies`) is not unit-testable, but
the pure logic around it — pulling the three cookies out of whatever shape a
webview backend returns, and classifying the verification probe — is.
"""

from collections.abc import Callable, Iterable
from http.cookies import SimpleCookie

from api_service import AuthExpiredError
from config import ConfigData
from login import extract_cookies, verify_cookies
from model import WinRateResponse

CAPTURED = {"buckler_id": "id", "buckler_r_id": "r", "buckler_praise_date": "123"}


class _CookiejarCookie:
    """Stand-in for an http.cookiejar.Cookie (has .name / .value)."""

    def __init__(self, name: str, value: str) -> None:
        self.name = name
        self.value = value


def _simplecookies(pairs: Iterable[tuple[str, str]]) -> list[SimpleCookie]:
    """A list of one-morsel SimpleCookies, as WebView2 returns them."""
    result = []
    for name, value in pairs:
        cookie: SimpleCookie = SimpleCookie()
        cookie[name] = value
        result.append(cookie)
    return result


def test_extract_cookies_from_simplecookie_list() -> None:
    cookies = _simplecookies(
        [
            ("buckler_id", "id"),
            ("buckler_r_id", "r"),
            ("buckler_praise_date", "123"),
            ("other", "ignored"),
        ]
    )
    assert extract_cookies(cookies) == CAPTURED


def test_extract_cookies_from_cookiejar_objects() -> None:
    cookies = [
        _CookiejarCookie("buckler_id", "id"),
        _CookiejarCookie("buckler_r_id", "r"),
        _CookiejarCookie("buckler_praise_date", "123"),
    ]
    assert extract_cookies(cookies) == CAPTURED


def test_extract_cookies_skips_empty_and_partial() -> None:
    cookies = _simplecookies([("buckler_id", "id"), ("buckler_r_id", "")])
    # buckler_r_id is empty (excluded) and buckler_praise_date is absent.
    assert extract_cookies(cookies) == {"buckler_id": "id"}


def test_extract_cookies_handles_none() -> None:
    assert extract_cookies(None) == {}


def test_verify_cookies_verified(make_config: Callable[..., ConfigData]) -> None:
    seen: dict[str, ConfigData] = {}

    def fetcher(config: ConfigData) -> WinRateResponse:
        seen["config"] = config
        return WinRateResponse(character_win_rates=[])

    assert verify_cookies(make_config(), CAPTURED, win_rate_fetcher=fetcher) == "verified"
    # The probe runs with the captured cookies, praise_date coerced to int.
    assert seen["config"].buckler_id == "id"
    assert seen["config"].buckler_praise_date == 123


def test_verify_cookies_rejected(make_config: Callable[..., ConfigData]) -> None:
    def fetcher(config: ConfigData) -> WinRateResponse:
        raise AuthExpiredError("expired")

    assert verify_cookies(make_config(), CAPTURED, win_rate_fetcher=fetcher) == "rejected"


def test_verify_cookies_unverified_on_other_error(
    make_config: Callable[..., ConfigData],
) -> None:
    def fetcher(config: ConfigData) -> WinRateResponse:
        raise RuntimeError("network down")

    assert verify_cookies(make_config(), CAPTURED, win_rate_fetcher=fetcher) == "unverified"
