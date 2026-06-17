"""
Runtime configuration loaded from environment variables.
All secrets are read from the environment — never hard-coded.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # ── AuditBoard ────────────────────────────────────────────────────────────
    auditboard_base_url: str = Field(..., description="e.g. https://tenant.auditboardapp.com")
    auditboard_api_token: str = Field(..., description="Bearer token for AuditBoard API")
    # AuditBoard exposes policies under /v1/policies (REST API v1).
    # Confirm the exact path in your tenant's API reference; change here if needed.
    auditboard_policy_endpoint: str = Field(default="/v1/policies")

    # ── ServiceNow ────────────────────────────────────────────────────────────
    servicenow_instance: str = Field(..., description="e.g. https://dev12345.service-now.com")
    servicenow_auth_mode: Literal["basic", "oauth"] = Field(default="basic")

    # basic auth
    servicenow_user: str | None = Field(default=None)
    servicenow_password: str | None = Field(default=None)

    # oauth
    servicenow_client_id: str | None = Field(default=None)
    servicenow_client_secret: str | None = Field(default=None)
    servicenow_token_url: str | None = Field(default=None)

    # KB target
    servicenow_kb_sys_id: str = Field(..., description="sys_id of the target knowledge base")
    servicenow_kb_category: str | None = Field(default=None, description="Default category sys_id")

    # ── Sync behaviour ────────────────────────────────────────────────────────
    sync_auto_publish: bool = Field(default=True)
    sync_page_size: int = Field(default=100, ge=1, le=1000)

    @field_validator("auditboard_base_url", "servicenow_instance", mode="before")
    @classmethod
    def strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @model_validator(mode="after")
    def check_auth_credentials(self) -> "Settings":
        if self.servicenow_auth_mode == "basic":
            missing = [f for f in ("servicenow_user", "servicenow_password") if not getattr(self, f)]
            if missing:
                raise ValueError(f"basic auth requires: {', '.join(missing)}")
        else:
            missing = [
                f
                for f in ("servicenow_client_id", "servicenow_client_secret", "servicenow_token_url")
                if not getattr(self, f)
            ]
            if missing:
                raise ValueError(f"oauth requires: {', '.join(missing)}")
        return self


def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
