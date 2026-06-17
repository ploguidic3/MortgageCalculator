"""
Core one-way sync logic: AuditBoard → ServiceNow KB.

Rules
-----
- Active policies (published / draft in AuditBoard) → create or update KB article.
- Archived / deleted policies → retire the corresponding KB article.
- Articles in ServiceNow that have no matching AuditBoard policy ID are left alone
  (they were not created by this sync).
- Never writes back to AuditBoard.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .auditboard_client import AuditBoardClient
from .config import Settings
from .models import AuditBoardPolicy
from .servicenow_client import ServiceNowClient

log = logging.getLogger(__name__)


@dataclass
class SyncResult:
    created: int = 0
    updated: int = 0
    retired: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def log_summary(self) -> None:
        log.info(
            "Sync complete",
            extra={
                "created": self.created,
                "updated": self.updated,
                "retired": self.retired,
                "skipped": self.skipped,
                "errors": len(self.errors),
            },
        )


def _policy_body(policy: AuditBoardPolicy) -> str:
    """Produce the KB article body from a policy object."""
    parts: list[str] = []
    if policy.description:
        parts.append(f"<p>{policy.description}</p>")
    if policy.content:
        parts.append(policy.content)
    return "\n".join(parts) or f"<p>Policy: {policy.name}</p>"


def run_sync(settings: Settings, dry_run: bool = False) -> SyncResult:
    result = SyncResult()

    with AuditBoardClient(
        base_url=settings.auditboard_base_url,
        api_token=settings.auditboard_api_token,
        page_size=settings.sync_page_size,
    ) as ab_client, ServiceNowClient(
        instance=settings.servicenow_instance,
        auth_mode=settings.servicenow_auth_mode,
        user=settings.servicenow_user,
        password=settings.servicenow_password,
        client_id=settings.servicenow_client_id,
        client_secret=settings.servicenow_client_secret,
        token_url=settings.servicenow_token_url,
        kb_sys_id=settings.servicenow_kb_sys_id,
        kb_category=settings.servicenow_kb_category,
        auto_publish=settings.sync_auto_publish,
    ) as sn_client:

        # ── Phase 1: collect all AuditBoard policies ──────────────────────────
        policies: list[AuditBoardPolicy] = []
        try:
            policies = list(ab_client.iter_policies(settings.auditboard_policy_endpoint))
        except Exception as exc:
            log.error("Failed to fetch AuditBoard policies", extra={"error": str(exc)})
            result.errors.append(f"AB fetch failed: {exc}")
            result.log_summary()
            return result

        log.info("Fetched AuditBoard policies", extra={"count": len(policies)})
        active_policy_ids: set[str] = set()

        # ── Phase 2: upsert active policies into ServiceNow ───────────────────
        for policy in policies:
            try:
                _upsert_policy(policy, sn_client, result, dry_run)
                if policy.is_active:
                    active_policy_ids.add(policy.id)
            except Exception as exc:
                log.error(
                    "Error syncing policy",
                    extra={"policy_id": policy.id, "policy_name": policy.name, "error": str(exc)},
                )
                result.errors.append(f"Policy {policy.id}: {exc}")

        # ── Phase 3: retire articles whose policy is no longer active ─────────
        try:
            _retire_orphans(active_policy_ids, sn_client, result, dry_run)
        except Exception as exc:
            log.error("Error during retirement sweep", extra={"error": str(exc)})
            result.errors.append(f"Retirement sweep: {exc}")

    result.log_summary()
    return result


def _upsert_policy(
    policy: AuditBoardPolicy,
    sn_client: ServiceNowClient,
    result: SyncResult,
    dry_run: bool,
) -> None:
    title = policy.name
    body = _policy_body(policy)

    existing = sn_client.find_article_by_policy_id(policy.id)

    if policy.is_retired:
        if existing and existing.workflow_state != "retired":
            log.info(
                "Retiring KB article (policy archived/deleted)",
                extra={"policy_id": policy.id, "article_sys_id": existing.sys_id, "dry_run": dry_run},
            )
            if not dry_run:
                sn_client.retire_article(existing.sys_id)
            result.retired += 1
        else:
            result.skipped += 1
        return

    if existing is None:
        log.info(
            "Creating KB article",
            extra={"policy_id": policy.id, "title": title, "dry_run": dry_run},
        )
        if not dry_run:
            article = sn_client.create_article(policy.id, title, body)
            log.info("Created", extra={"sys_id": article.sys_id, "number": article.number})
        result.created += 1
    else:
        # Only update if something meaningful changed.
        if existing.short_description == title and (existing.text or "") == body:
            log.debug("No changes for policy", extra={"policy_id": policy.id})
            result.skipped += 1
            return
        log.info(
            "Updating KB article",
            extra={"policy_id": policy.id, "article_sys_id": existing.sys_id, "dry_run": dry_run},
        )
        if not dry_run:
            sn_client.update_article(existing.sys_id, title, body)
        result.updated += 1


def _retire_orphans(
    active_policy_ids: set[str],
    sn_client: ServiceNowClient,
    result: SyncResult,
    dry_run: bool,
) -> None:
    """Retire any KB articles that reference a policy ID not in the active set."""
    articles = sn_client.list_articles_for_kb()
    for article in articles:
        if not article.u_auditboard_policy_id:
            continue
        if article.u_auditboard_policy_id in active_policy_ids:
            continue
        if article.workflow_state == "retired":
            result.skipped += 1
            continue
        log.info(
            "Retiring orphaned KB article",
            extra={
                "policy_id": article.u_auditboard_policy_id,
                "article_sys_id": article.sys_id,
                "dry_run": dry_run,
            },
        )
        if not dry_run:
            sn_client.retire_article(article.sys_id)
        result.retired += 1
