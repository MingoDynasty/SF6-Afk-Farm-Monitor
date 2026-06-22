import logging
from typing import Any
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

PUSHOVER_API_BASE_URL = "https://api.pushover.net/1"
REQUEST_TIMEOUT = (10, 30)

JsonDict = dict[str, Any]


class PushoverClient:
    def __init__(
        self,
        app_key: str,
        user_key: str,
        base_url: str = PUSHOVER_API_BASE_URL,
        timeout: tuple[int, int] = REQUEST_TIMEOUT,
    ) -> None:
        self.app_key = app_key
        self.user_key = user_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # Most recent X-Limit-App-Remaining seen on any response, so the
        # IncidentManager can open a low-quota incident before sends start
        # failing (ALERT_DEDUPLICATION_PROPOSAL.md §9.2). None until first call.
        self.last_remaining: int | None = None

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
        payload: JsonDict = {
            "token": self.app_key,
            "user": self.user_key,
            "message": message,
            "priority": priority,
        }
        optional_fields = {
            "retry": retry,
            "expire": expire,
            "tags": tags,
            "sound": sound,
            "url": url,
            "url_title": url_title,
            "timestamp": timestamp,
        }
        payload.update(
            {key: value for key, value in optional_fields.items() if value is not None}
        )

        response_json = self._post("messages.json", payload)
        if response_json is None:
            return None

        receipt = response_json.get("receipt")
        if receipt is None:
            return None
        return str(receipt)

    def check_receipt(self, receipt: str) -> JsonDict:
        encoded_receipt = quote(receipt, safe="")
        response_json = self._get(
            f"receipts/{encoded_receipt}.json", {"token": self.app_key}
        )
        if response_json is None:
            return {}
        return response_json

    def cancel(self, receipt: str) -> bool:
        """Cancel an emergency retry by receipt.

        Returns True when the cancel is *settled* and the receipt can be
        forgotten: either Pushover accepted it, or it reports the receipt gone
        (a 4xx such as the 404 "receipt not found; may be invalid or expired"
        Pushover returns once the emergency window has elapsed — it is no longer
        nagging anyone, so there is nothing left to cancel). Returns False only
        on a transient failure (network error or 5xx) worth retrying next poll,
        so a stale receipt never wedges ``pending_cancel`` into logging 404s
        forever.
        """
        encoded_receipt = quote(receipt, safe="")
        path = f"receipts/{encoded_receipt}/cancel.json"
        response = self._request("POST", path, data={"token": self.app_key})
        if response is None:
            return False  # network failure: retry next poll
        if 400 <= response.status_code < 500:
            logger.info(
                "Pushover cancel for %s returned HTTP %s; receipt already gone, "
                "treating as cancelled.",
                path,
                response.status_code,
            )
            return True
        return self._response_json(response, "POST", path) is not None

    def cancel_by_tag(self, tag: str) -> bool:
        encoded_tag = quote(tag, safe="")
        return (
            self._post(
                f"receipts/cancel_by_tag/{encoded_tag}.json",
                {"token": self.app_key},
            )
            is not None
        )

    def _get(self, path: str, params: JsonDict) -> JsonDict | None:
        return self._request_json("GET", path, params=params)

    def _post(self, path: str, data: JsonDict) -> JsonDict | None:
        return self._request_json("POST", path, data=data)

    def _request_json(
        self,
        method: str,
        path: str,
        data: JsonDict | None = None,
        params: JsonDict | None = None,
    ) -> JsonDict | None:
        response = self._request(method, path, data=data, params=params)
        if response is None:
            return None
        return self._response_json(response, method, path)

    def _request(
        self,
        method: str,
        path: str,
        data: JsonDict | None = None,
        params: JsonDict | None = None,
    ) -> requests.Response | None:
        """Perform the HTTP call and record the quota header. Returns the
        response, or None on a network-level failure (always transient)."""
        url = f"{self.base_url}/{path}"

        try:
            if method == "GET":
                response = requests.get(url, params=params, timeout=self.timeout)
            else:
                response = requests.post(url, data=data, timeout=self.timeout)
        except requests.RequestException as exc:
            logger.error(
                "Pushover %s request failed for %s: %s",
                method,
                path,
                exc.__class__.__name__,
            )
            return None

        remaining = response.headers.get("X-Limit-App-Remaining")
        if remaining is not None:
            try:
                self.last_remaining = int(remaining)
            except ValueError:
                pass
            logger.info(
                "Pushover monthly quota: %s messages remaining.",
                remaining,
            )

        return response

    def _response_json(
        self, response: requests.Response, method: str, path: str
    ) -> JsonDict | None:
        try:
            response_json = response.json()
        except ValueError:
            logger.error(
                "Pushover %s request for %s returned non-JSON response: HTTP %s",
                method,
                path,
                response.status_code,
            )
            return None

        if not isinstance(response_json, dict):
            logger.error(
                "Pushover %s request for %s returned non-object JSON: HTTP %s",
                method,
                path,
                response.status_code,
            )
            return None

        if response.status_code != 200 or response_json.get("status") != 1:
            logger.error(
                "Pushover %s request for %s failed: HTTP %s status=%s errors=%s request=%s",
                method,
                path,
                response.status_code,
                response_json.get("status"),
                response_json.get("errors"),
                response_json.get("request"),
            )
            return None

        return response_json
