"""Unit tests for sync logic using mocked clients."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from ab_snow_sync.models import AuditBoardPolicy, KBArticle
from ab_snow_sync.sync import SyncResult, _upsert_policy, run_sync, _retire_orphans


def _policy(id: str, status: str = "published", name: str = "Test Policy", content: str = "body") -> AuditBoardPolicy:
    return AuditBoardPolicy(id=id, name=name, status=status, content=content)


def _article(sys_id: str, policy_id: str, state: str = "published", title: str = "Test Policy", body: str = "body") -> KBArticle:
    return KBArticle(
        sys_id=sys_id,
        number="KB0001",
        short_description=title,
        text=body,
        workflow_state=state,
        u_auditboard_policy_id=policy_id,
    )


class TestUpsertPolicy:
    def setup_method(self):
        self.sn = MagicMock()

    def test_creates_article_when_none_exists(self):
        self.sn.find_article_by_policy_id.return_value = None
        result = SyncResult()
        _upsert_policy(_policy("p1"), self.sn, result, dry_run=False)
        self.sn.create_article.assert_called_once()
        assert result.created == 1

    def test_skips_create_in_dry_run(self):
        self.sn.find_article_by_policy_id.return_value = None
        result = SyncResult()
        _upsert_policy(_policy("p1"), self.sn, result, dry_run=True)
        self.sn.create_article.assert_not_called()
        assert result.created == 1  # counted even in dry-run

    def test_updates_article_when_content_changed(self):
        existing = _article("s1", "p1", body="old body")
        self.sn.find_article_by_policy_id.return_value = existing
        result = SyncResult()
        _upsert_policy(_policy("p1", content="new body"), self.sn, result, dry_run=False)
        self.sn.update_article.assert_called_once_with("s1", "Test Policy", "new body")
        assert result.updated == 1

    def test_skips_article_when_unchanged(self):
        policy = _policy("p1", content="body")
        body = "body"  # _policy_body returns content as-is when no description
        existing = _article("s1", "p1", body=body)
        self.sn.find_article_by_policy_id.return_value = existing
        result = SyncResult()
        _upsert_policy(policy, self.sn, result, dry_run=False)
        self.sn.update_article.assert_not_called()
        assert result.skipped == 1

    def test_retires_article_for_archived_policy(self):
        existing = _article("s1", "p1", state="published")
        self.sn.find_article_by_policy_id.return_value = existing
        result = SyncResult()
        _upsert_policy(_policy("p1", status="archived"), self.sn, result, dry_run=False)
        self.sn.retire_article.assert_called_once_with("s1")
        assert result.retired == 1

    def test_skips_already_retired_article(self):
        existing = _article("s1", "p1", state="retired")
        self.sn.find_article_by_policy_id.return_value = existing
        result = SyncResult()
        _upsert_policy(_policy("p1", status="archived"), self.sn, result, dry_run=False)
        self.sn.retire_article.assert_not_called()
        assert result.skipped == 1

    def test_skips_retirement_in_dry_run(self):
        existing = _article("s1", "p1", state="published")
        self.sn.find_article_by_policy_id.return_value = existing
        result = SyncResult()
        _upsert_policy(_policy("p1", status="deleted"), self.sn, result, dry_run=True)
        self.sn.retire_article.assert_not_called()
        assert result.retired == 1


class TestRetireOrphans:
    def setup_method(self):
        self.sn = MagicMock()

    def test_retires_article_not_in_active_set(self):
        self.sn.list_articles_for_kb.return_value = [_article("s1", "p_gone", state="published")]
        result = SyncResult()
        _retire_orphans(active_policy_ids=set(), sn_client=self.sn, result=result, dry_run=False)
        self.sn.retire_article.assert_called_once_with("s1")
        assert result.retired == 1

    def test_skips_article_in_active_set(self):
        self.sn.list_articles_for_kb.return_value = [_article("s1", "p1", state="published")]
        result = SyncResult()
        _retire_orphans(active_policy_ids={"p1"}, sn_client=self.sn, result=result, dry_run=False)
        self.sn.retire_article.assert_not_called()

    def test_skips_already_retired_orphan(self):
        self.sn.list_articles_for_kb.return_value = [_article("s1", "p_gone", state="retired")]
        result = SyncResult()
        _retire_orphans(active_policy_ids=set(), sn_client=self.sn, result=result, dry_run=False)
        self.sn.retire_article.assert_not_called()
        assert result.skipped == 1


class TestRunSync:
    def test_run_sync_ab_failure_returns_error(self):
        from ab_snow_sync.config import Settings

        settings = Settings(
            auditboard_base_url="https://ab.example.com",
            auditboard_api_token="tok",
            servicenow_instance="https://sn.example.com",
            servicenow_kb_sys_id="kb1",
            servicenow_user="u",
            servicenow_password="p",
        )
        with (
            patch("ab_snow_sync.sync.AuditBoardClient") as MockAB,
            patch("ab_snow_sync.sync.ServiceNowClient") as MockSN,
        ):
            ab_instance = MagicMock()
            ab_instance.__enter__ = MagicMock(return_value=ab_instance)
            ab_instance.__exit__ = MagicMock(return_value=False)
            ab_instance.iter_policies.side_effect = RuntimeError("network error")
            MockAB.return_value = ab_instance

            sn_instance = MagicMock()
            sn_instance.__enter__ = MagicMock(return_value=sn_instance)
            sn_instance.__exit__ = MagicMock(return_value=False)
            MockSN.return_value = sn_instance

            result = run_sync(settings, dry_run=True)
            assert len(result.errors) == 1
            assert "AB fetch failed" in result.errors[0]
