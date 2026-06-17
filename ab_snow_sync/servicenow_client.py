"""ServiceNow Table API client for kb_knowledge with basic/OAuth auth."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import httpx
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from .models import KBArticle

log = logging.getLogger(__name__)

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_KB_TABLE = "kb_knowledge"
# Fields we read back after create/update.
_SYSPARM_FIELDS = "sys_id,number,short_description,text,workflow_state,u_auditboard_policy_id"


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_STATUS
    return isinstance(exc, (httpx.TimeoutException, httpx.NetworkError))


class _OAuthTokenManager:
    """Thread-safe OAuth2 client-credentials token manager."""

    def __init__(self, client_id: str, client_secret: str, token_url: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_url = token_url
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._lock = threading.Lock()

    def get_token(self) -> str:
        with self._lock:
            if self._token and time.monotonic() < self._expires_at - 30:
                return self._token
            self._refresh()
            assert self._token
            return self._token

    def _refresh(self) -> None:
        log.info("Refreshing ServiceNow OAuth token")
        resp = httpx.post(
            self._token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            timeout=15,
        )
        resp.raise_for_status()
        body = resp.json()
        self._token = body["access_token"]
        self._expires_at = time.monotonic() + int(body.get("expires_in", 1800))


class ServiceNowClient:
    def __init__(
        self,
        instance: str,
        *,
        auth_mode: str,
        user: str | None = None,
        password: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        token_url: str | None = None,
        kb_sys_id: str,
        kb_category: str | None = None,
        auto_publish: bool = True,
    ) -> None:
        self._instance = instance
        self._kb_sys_id = kb_sys_id
        self._kb_category = kb_category
        self._auto_publish = auto_publish
        self._auth_mode = auth_mode
        self._oauth: _OAuthTokenManager | None = None

        auth: httpx.Auth | None = None
        if auth_mode == "basic":
            auth = httpx.BasicAuth(user or "", password or "")
        else:
            self._oauth = _OAuthTokenManager(client_id or "", client_secret or "", token_url or "")

        self._http = httpx.Client(
            base_url=instance,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            auth=auth,
            timeout=30,
        )

    def _auth_headers(self) -> dict[str, str]:
        if self._oauth:
            return {"Authorization": f"Bearer {self._oauth.get_token()}"}
        return {}

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "ServiceNowClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ── low-level HTTP helpers ────────────────────────────────────────────────

    def _url(self, path: str) -> str:
        return f"/api/now/table/{path}"

    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        stop=stop_after_attempt(5),
        before_sleep=before_sleep_log(log, logging.WARNING),
        reraise=True,
    )
    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        resp = self._http.request(method, path, headers=self._auth_headers(), **kwargs)
        resp.raise_for_status()
        return resp

    # ── KB article helpers ────────────────────────────────────────────────────

    def find_article_by_policy_id(self, policy_id: str) -> KBArticle | None:
        """Return the first KB article whose u_auditboard_policy_id matches."""
        resp = self._request(
            "GET",
            self._url(_KB_TABLE),
            params={
                "sysparm_query": f"u_auditboard_policy_id={policy_id}^kb_knowledge_base={self._kb_sys_id}",
                "sysparm_fields": _SYSPARM_FIELDS,
                "sysparm_limit": 1,
            },
        )
        results: list[dict[str, Any]] = resp.json().get("result", [])
        if not results:
            return None
        return _parse_article(results[0])

    def list_articles_for_kb(self) -> list[KBArticle]:
        """Return all articles in our KB that have a policy ID set (for retirement sweep)."""
        articles: list[KBArticle] = []
        offset = 0
        limit = 200
        while True:
            resp = self._request(
                "GET",
                self._url(_KB_TABLE),
                params={
                    "sysparm_query": (
                        f"kb_knowledge_base={self._kb_sys_id}"
                        "^u_auditboard_policy_idISNOTEMPTY"
                    ),
                    "sysparm_fields": _SYSPARM_FIELDS,
                    "sysparm_limit": limit,
                    "sysparm_offset": offset,
                },
            )
            results: list[dict[str, Any]] = resp.json().get("result", [])
            articles.extend(_parse_article(r) for r in results)
            if len(results) < limit:
                break
            offset += limit
        return articles

    def create_article(self, policy_id: str, title: str, body: str) -> KBArticle:
        payload = self._build_payload(title, body, policy_id)
        resp = self._request("POST", self._url(_KB_TABLE), json=payload)
        return _parse_article(resp.json()["result"])

    def update_article(self, sys_id: str, title: str, body: str) -> KBArticle:
        payload = self._build_payload(title, body)
        resp = self._request("PATCH", self._url(f"{_KB_TABLE}/{sys_id}"), json=payload)
        return _parse_article(resp.json()["result"])

    def retire_article(self, sys_id: str) -> None:
        """Set workflow_state to 'retired' to mark the article as inactive."""
        self._request("PATCH", self._url(f"{_KB_TABLE}/{sys_id}"), json={"workflow_state": "retired"})

    def _build_payload(self, title: str, body: str, policy_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "short_description": title,
            "text": body,
            "kb_knowledge_base": self._kb_sys_id,
            "workflow_state": "published" if self._auto_publish else "draft",
        }
        if self._kb_category:
            payload["kb_category"] = self._kb_category
        if policy_id is not None:
            payload["u_auditboard_policy_id"] = policy_id
        return payload


def _parse_article(raw: dict[str, Any]) -> KBArticle:
    return KBArticle(
        sys_id=raw.get("sys_id", ""),
        number=raw.get("number", ""),
        short_description=raw.get("short_description", ""),
        text=raw.get("text"),
        workflow_state=raw.get("workflow_state", "draft"),
        u_auditboard_policy_id=raw.get("u_auditboard_policy_id") or None,
    )
