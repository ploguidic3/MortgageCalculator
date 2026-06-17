"""Config validation tests."""

import pytest
from pydantic import ValidationError

from ab_snow_sync.config import Settings


BASE = {
    "auditboard_base_url": "https://tenant.auditboardapp.com",
    "auditboard_api_token": "tok",
    "servicenow_instance": "https://dev.service-now.com",
    "servicenow_kb_sys_id": "abc123",
}


def _make(**overrides):
    return {**BASE, **overrides}


def test_basic_auth_valid():
    s = Settings(**_make(servicenow_auth_mode="basic", servicenow_user="u", servicenow_password="p"))
    assert s.servicenow_user == "u"


def test_basic_auth_missing_password():
    with pytest.raises((ValidationError, ValueError)):
        Settings(**_make(servicenow_auth_mode="basic", servicenow_user="u"))


def test_oauth_valid():
    s = Settings(
        **_make(
            servicenow_auth_mode="oauth",
            servicenow_client_id="id",
            servicenow_client_secret="secret",
            servicenow_token_url="https://dev.service-now.com/oauth_token.do",
        )
    )
    assert s.servicenow_auth_mode == "oauth"


def test_oauth_missing_fields():
    with pytest.raises((ValidationError, ValueError)):
        Settings(**_make(servicenow_auth_mode="oauth", servicenow_client_id="id"))


def test_trailing_slash_stripped():
    s = Settings(**_make(auditboard_base_url="https://tenant.auditboardapp.com/", servicenow_user="u", servicenow_password="p"))
    assert not s.auditboard_base_url.endswith("/")


def test_defaults():
    s = Settings(**_make(servicenow_user="u", servicenow_password="p"))
    assert s.sync_page_size == 100
    assert s.sync_auto_publish is True
    assert s.auditboard_policy_endpoint == "/v1/policies"
