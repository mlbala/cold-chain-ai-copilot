"""Applies the least-privilege security setup (`fde-copilot setup-rbac`)."""

from __future__ import annotations

from importlib import resources

from psycopg import sql

from cold_chain_copilot.core.config import Settings
from cold_chain_copilot.core.logging import get_logger
from cold_chain_copilot.db.pool import direct_connection

logger = get_logger(__name__)

MIGRATION_FILE = "001_security_setup.sql"


def load_migration_sql() -> str:
    """The packaged migration text (works from a wheel as well as a checkout)."""
    return (
        resources.files("cold_chain_copilot.db")
        .joinpath("migrations", MIGRATION_FILE)
        .read_text(encoding="utf-8")
    )


def apply_security_setup(settings: Settings) -> None:
    """Create or re-key `usr_fde_ro`, then apply the idempotent DDL and grants atomically."""
    username, password = settings.agent_role_credentials()

    with direct_connection(settings.secret("aiven_admin_db_uri")) as conn, conn.transaction():
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (username,)).fetchone()
        verb = "ALTER" if exists else "CREATE"
        conn.execute(
            sql.SQL(
                verb + " ROLE {role} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOREPLICATION NOBYPASSRLS PASSWORD {password}"
            ).format(role=sql.Identifier(username), password=sql.Literal(password))
        )
        logger.info("agent_role_ready", role=username, action=verb.lower())

        conn.execute(load_migration_sql().encode("utf-8"))
        logger.info("security_migration_applied", migration=MIGRATION_FILE)
