import logging
from typing import Any
from urllib.parse import quote

import requests  # type: ignore[import-untyped]

from config import config

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
        encoded_receipt = quote(receipt, safe="")
        return (
            self._post(
                f"receipts/{encoded_receipt}/cancel.json", {"token": self.app_key}
            )
            is not None
        )

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


pushover_client = (
    PushoverClient(config.pushover_app_key, config.pushover_user_key)
    if config.pushover_enabled
    else None
)


def send_message(message: str) -> None:
    if pushover_client is None:
        return
    pushover_client.send(message)
