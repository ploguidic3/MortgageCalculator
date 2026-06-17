"""Unit tests for ServiceNow client."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from ab_snow_sync.servicenow_client import ServiceNowClient, _parse_article


def _sn_client(**kwargs) -> ServiceNowClient:
    defaults = dict(
        instance="https://dev.service-now.com",
        auth_mode="basic",
        user="admin",
        password="pass",
        kb_sys_id="kb1",
        auto_publish=True,
    )
    return ServiceNowClient(**{**defaults, **kwargs})


def _resp(body: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(body).encode(), headers={"content-type": "application/json"})


class TestParseArticle:
    def test_parses_full(self):
        raw = {
            "sys_id": "s1",
            "number": "KB0001",
            "short_description": "Title",
            "text": "<p>body</p>",
            "workflow_state": "published",
            "u_auditboard_policy_id": "p1",
        }
        a = _parse_article(raw)
        assert a.sys_id == "s1"
        assert a.u_auditboard_policy_id == "p1"

    def test_parses_empty_policy_id_as_none(self):
        a = _parse_article({"sys_id": "s1", "number": "KB0001", "short_description": "T", "u_auditboard_policy_id": ""})
        assert a.u_auditboard_policy_id is None


class TestBuildPayload:
    def test_auto_publish_true(self):
        c = _sn_client(auto_publish=True)
        p = c._build_payload("Title", "body", "p1")
        assert p["workflow_state"] == "published"
        assert p["u_auditboard_policy_id"] == "p1"

    def test_auto_publish_false(self):
        c = _sn_client(auto_publish=False)
        p = c._build_payload("Title", "body")
        assert p["workflow_state"] == "draft"
        assert "u_auditboard_policy_id" not in p

    def test_includes_category_when_set(self):
        c = _sn_client(kb_category="cat1")
        p = c._build_payload("T", "b")
        assert p["kb_category"] == "cat1"


class TestFindArticle:
    def test_returns_none_when_not_found(self):
        c = _sn_client()
        with patch.object(c, "_request", return_value=_resp({"result": []})):
            assert c.find_article_by_policy_id("p1") is None

    def test_returns_article_when_found(self):
        c = _sn_client()
        raw = {
            "sys_id": "s1", "number": "KB0001", "short_description": "T",
            "workflow_state": "published", "u_auditboard_policy_id": "p1",
        }
        with patch.object(c, "_request", return_value=_resp({"result": [raw]})):
            a = c.find_article_by_policy_id("p1")
        assert a is not None
        assert a.sys_id == "s1"
