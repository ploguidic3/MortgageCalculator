"""Unit tests for AuditBoard client pagination."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from ab_snow_sync.auditboard_client import AuditBoardClient


def _make_response(items: list[dict], status_code: int = 200) -> httpx.Response:
    import json

    return httpx.Response(status_code, content=json.dumps({"data": items}).encode(), headers={"content-type": "application/json"})


def test_iter_policies_single_page():
    policy_data = [{"id": "1", "name": "Policy A", "status": "published", "content": "text"}]
    with patch.object(AuditBoardClient, "_get_page", return_value={"data": policy_data}):
        client = AuditBoardClient("https://ab.example.com", "tok", page_size=100)
        policies = list(client.iter_policies("/v1/policies"))
    assert len(policies) == 1
    assert policies[0].id == "1"
    assert policies[0].name == "Policy A"


def test_iter_policies_paginates():
    page1 = [{"id": str(i), "name": f"P{i}", "status": "published"} for i in range(3)]
    page2 = [{"id": "99", "name": "Last", "status": "archived"}]

    call_count = 0

    def fake_get_page(endpoint, params):
        nonlocal call_count
        call_count += 1
        return {"data": page1 if call_count == 1 else page2}

    with patch.object(AuditBoardClient, "_get_page", side_effect=fake_get_page):
        client = AuditBoardClient("https://ab.example.com", "tok", page_size=3)
        policies = list(client.iter_policies("/v1/policies"))

    assert len(policies) == 4
    assert call_count == 2


def test_iter_policies_empty_response():
    with patch.object(AuditBoardClient, "_get_page", return_value={"data": []}):
        client = AuditBoardClient("https://ab.example.com", "tok")
        policies = list(client.iter_policies("/v1/policies"))
    assert policies == []


def test_iter_policies_top_level_list():
    """Support AuditBoard endpoints that return a bare list instead of {data:[...]}."""
    items = [{"id": "1", "name": "P", "status": "draft"}]
    with patch.object(AuditBoardClient, "_get_page", return_value=items):
        client = AuditBoardClient("https://ab.example.com", "tok")
        policies = list(client.iter_policies("/v1/policies"))
    assert len(policies) == 1
