"""Shared data models for the AuditBoard → ServiceNow sync."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class PolicyStatus(str, Enum):
    PUBLISHED = "published"
    DRAFT = "draft"
    ARCHIVED = "archived"
    DELETED = "deleted"


class AuditBoardPolicy(BaseModel):
    id: str
    name: str
    status: str  # raw string; we map to PolicyStatus in sync logic
    content: str | None = Field(default=None)
    description: str | None = Field(default=None)
    # Keep the raw payload for extensibility without breaking the model.
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_active(self) -> bool:
        return self.status.lower() in (PolicyStatus.PUBLISHED, PolicyStatus.DRAFT)

    @property
    def is_retired(self) -> bool:
        return self.status.lower() in (PolicyStatus.ARCHIVED, PolicyStatus.DELETED)


class KBArticle(BaseModel):
    sys_id: str
    number: str
    short_description: str
    text: str | None = None
    workflow_state: str = "draft"
    # We store the AuditBoard policy ID in a custom field for idempotent matching.
    # ServiceNow field: u_auditboard_policy_id (must be created as a string field
    # on kb_knowledge in your SN instance).
    u_auditboard_policy_id: str | None = None
