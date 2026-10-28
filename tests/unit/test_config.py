"""Database settings: separate AIVEN_* fields, URI fallback, and least-privilege checks."""

from __future__ import annotations

from typing import Any

import pytest
from psycopg.conninfo import conninfo_to_dict
from pydantic import SecretStr

from cold_chain_copilot.core.config import Settings
from cold_chain_copilot.core.exceptions import ConfigurationError

HOST = "pg-example.aivencloud.com"


def settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def separate(**overrides: Any) -> Settings:
    fields: dict[str, Any] = {
        "aiven_host": HOST,
        "aiven_port": 26563,
        "aiven_db_name": "defaultdb",
        "aiven_admin_password": SecretStr("admin-password"),
        "aiven_agent_ro_password": SecretStr("agent-password-123"),
    }
    fields.update(overrides)
    return settings(**fields)


def test_separate_fields_build_both_connections() -> None:
    s = separate()
    admin = conninfo_to_dict(s.conninfo("admin"))
    agent = conninfo_to_dict(s.conninfo("agent"))
    assert admin == {
        "host": HOST,
        "port": "26563",
        "dbname": "defaultdb",
        "user": "avnadmin",
        "password": "admin-password",
        "sslmode": "require",
    }
    assert agent["user"] == "usr_fde_ro"
    assert agent["password"] == "agent-password-123"
    assert s.agent_role_credentials() == ("usr_fde_ro", "agent-password-123")
    assert s.missing_db_settings() == []


def test_separate_fields_win_over_uris() -> None:
    s = separate(aiven_admin_db_uri=SecretStr("postgres://other:pw@elsewhere:5432/x"))
    assert conninfo_to_dict(s.conninfo("admin"))["host"] == HOST


def test_uri_fallback_when_host_is_not_set() -> None:
    s = settings(
        aiven_admin_db_uri=SecretStr("postgres://avnadmin:pw@db.example:5432/d?sslmode=require"),
        aiven_agent_ro_db_uri=SecretStr("postgres://usr_fde_ro:ro-password-123@db.example:5432/d"),
    )
    assert s.db_params("admin")["host"] == "db.example"
    assert s.db_params("admin")["sslmode"] == "require"
    assert s.agent_role_credentials() == ("usr_fde_ro", "ro-password-123")


def test_passwords_with_special_characters_are_quoted() -> None:
    tricky = "p@ss w'ord=\\#1!"
    s = separate(aiven_admin_password=SecretStr(tricky))
    assert conninfo_to_dict(s.conninfo("admin"))["password"] == tricky


def test_missing_port_and_passwords_are_reported() -> None:
    s = settings(aiven_host=HOST)
    assert s.missing_db_settings() == [
        "AIVEN_PORT",
        "AIVEN_ADMIN_PASSWORD",
        "AIVEN_AGENT_RO_PASSWORD",
    ]
    with pytest.raises(ConfigurationError, match="AIVEN_PORT"):
        s.conninfo("admin")


def test_uri_mode_reports_missing_uris() -> None:
    assert settings().missing_db_settings() == ["AIVEN_ADMIN_DB_URI", "AIVEN_AGENT_RO_DB_URI"]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"aiven_agent_ro_user": "avnadmin"}, "must be 'usr_fde_ro'"),
        ({"aiven_agent_ro_password": SecretStr("short")}, "at least 12 characters"),
    ],
)
def test_agent_role_must_be_least_privilege(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        separate(**overrides).agent_role_credentials()
