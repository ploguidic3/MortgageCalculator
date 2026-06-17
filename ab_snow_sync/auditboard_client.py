"""AuditBoard API client with pagination and automatic retries."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import httpx
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from .models import AuditBoardPolicy

log = logging.getLogger(__name__)

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_STATUS
    return isinstance(exc, (httpx.TimeoutException, httpx.NetworkError))


class AuditBoardClient:
    def __init__(self, base_url: str, api_token: str, page_size: int = 100) -> None:
        self._base_url = base_url
        self._page_size = page_size
        self._http = httpx.Client(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {api_token}",
                "Accept": "application/json",
            },
            timeout=30,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "AuditBoardClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(5),
        before_sleep=before_sleep_log(log, logging.WARNING),
        reraise=True,
    )
    def _get_page(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        resp = self._http.get(endpoint, params=params)
        resp.raise_for_status()
        return resp.json()

    def iter_policies(self, endpoint: str) -> Iterator[AuditBoardPolicy]:
        """
        Yields all policies from AuditBoard, handling cursor- or offset-based
        pagination transparently.

        AuditBoard REST API v1 supports offset/limit query params.
        If the response envelope changes, adjust the key names below.
        """
        offset = 0
        while True:
            params: dict[str, Any] = {"limit": self._page_size, "offset": offset}
            log.info("Fetching AuditBoard policies", extra={"offset": offset, "limit": self._page_size})
            data = self._get_page(endpoint, params)

            # Support both {"data": [...]} and top-level list responses.
            items: list[dict[str, Any]] = data if isinstance(data, list) else data.get("data", [])

            if not items:
                break

            for raw in items:
                yield AuditBoardPolicy(
                    id=str(raw["id"]),
                    name=raw.get("name") or raw.get("title") or "",
                    status=raw.get("status") or "draft",
                    content=raw.get("content") or raw.get("body"),
                    description=raw.get("description"),
                    raw=raw,
                )

            if len(items) < self._page_size:
                break
            offset += self._page_size
