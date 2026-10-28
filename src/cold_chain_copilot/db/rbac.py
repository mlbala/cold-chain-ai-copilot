"""Applies the least-privilege security setup (`fde-copilot setup-rbac`)."""

from __future__ import annotations

from importlib import resources

from psycopg import sql

from cold_chain_copilot.core.config import Settings
from cold_chain_copilot.core.exceptions import ConfigurationError
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


_ELEVATED_ATTRIBUTES = (
    "rolsuper",
    "rolcreatedb",
    "rolcreaterole",
    "rolreplication",
    "rolbypassrls",
)


def apply_security_setup(settings: Settings) -> None:
    """Create or re-key `usr_fde_ro`, then apply the idempotent DDL and grants atomically.

    A new role is created with every elevated attribute explicitly off. An existing role only
    has its password re-keyed: PostgreSQL 16+ lets a non-superuser admin (Aiven's avnadmin)
    create roles but not touch attributes such as SUPERUSER, even to keep them off. Instead,
    an existing role that carries any elevated attribute aborts the setup.
    """
    username, password = settings.agent_role_credentials()

    with direct_connection(settings.conninfo("admin")) as conn, conn.transaction():
        existing = conn.execute(
            sql.SQL("SELECT {} FROM pg_roles WHERE rolname = %s").format(
                sql.SQL(", ").join(sql.Identifier(attr) for attr in _ELEVATED_ATTRIBUTES)
            ),
            (username,),
        ).fetchone()
        params = {"role": sql.Identifier(username), "password": sql.Literal(password)}
        if existing is None:
            conn.execute(
                sql.SQL(
                    "CREATE ROLE {role} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOREPLICATION NOBYPASSRLS PASSWORD {password}"
                ).format(**params)
            )
            action = "created"
        else:
            elevated = [attr for attr, on in zip(_ELEVATED_ATTRIBUTES, existing, strict=True) if on]
            if elevated:
                raise ConfigurationError(
                    f"Role {username} has elevated attributes {elevated}; a superuser must "
                    "remove them before it can serve as the least-privilege agent role."
                )
            conn.execute(
                sql.SQL("ALTER ROLE {role} WITH LOGIN PASSWORD {password}").format(**params)
            )
            action = "rekeyed"
        logger.info("agent_role_ready", role=username, action=action)

        conn.execute(load_migration_sql().encode("utf-8"))
        logger.info("security_migration_applied", migration=MIGRATION_FILE)
