"""psycopg v3 connection pools with strict role isolation.

Two pools never share credentials:

* `ro`    — `usr_fde_ro`: SELECT on `fde_views.vw_active_fleet`, INSERT on the audit log.
            Every agent query and the KPI ribbon run here.
* `admin` — `avnadmin`: migrations, seed ingestion, and the password-gated audit viewer.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal

import psycopg
from psycopg.rows import TupleRow
from psycopg_pool import ConnectionPool

from cold_chain_copilot.core.config import Settings
from cold_chain_copilot.core.logging import get_logger

logger = get_logger(__name__)

PoolName = Literal["ro", "admin"]

_CONNECT_TIMEOUT_S = 10
_APPLICATION_NAME = "cold-chain-copilot"
_CONNECT_KWARGS: dict[str, Any] = {
    "connect_timeout": _CONNECT_TIMEOUT_S,
    "application_name": _APPLICATION_NAME,
}


class DatabasePools:
    """Owns both pools for the lifetime of the API process."""

    def __init__(
        self,
        *,
        ro_conninfo: str,
        admin_conninfo: str,
        ro_max_size: int = 4,
        admin_max_size: int = 2,
    ) -> None:
        self.ro: ConnectionPool = ConnectionPool(
            conninfo=ro_conninfo,
            min_size=1,
            max_size=ro_max_size,
            kwargs=dict(_CONNECT_KWARGS),
            check=ConnectionPool.check_connection,
            name="fde-ro",
            open=False,
        )
        self.admin: ConnectionPool = ConnectionPool(
            conninfo=admin_conninfo,
            min_size=0,
            max_size=admin_max_size,
            kwargs=dict(_CONNECT_KWARGS),
            check=ConnectionPool.check_connection,
            name="fde-admin",
            open=False,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> DatabasePools:
        return cls(
            ro_conninfo=settings.secret("aiven_agent_ro_db_uri"),
            admin_conninfo=settings.secret("aiven_admin_db_uri"),
            ro_max_size=settings.db_pool_ro_max_size,
            admin_max_size=settings.db_pool_admin_max_size,
        )

    def open(self) -> None:
        """Open without blocking startup; connections are established in the background."""
        self.ro.open(wait=False)
        self.admin.open(wait=False)
        logger.info("db_pools_opened", ro_max=self.ro.max_size, admin_max=self.admin.max_size)

    def close(self) -> None:
        self.ro.close()
        self.admin.close()
        logger.info("db_pools_closed")

    def ping(self, pool: PoolName = "ro", timeout: float = 3.0) -> bool:
        """True when a connection can be checked out and answers `SELECT 1`."""
        target = self.ro if pool == "ro" else self.admin
        try:
            with target.connection(timeout=timeout) as conn:
                conn.execute("SELECT 1")
        except (psycopg.Error, TimeoutError) as exc:
            logger.warning("db_ping_failed", pool=pool, error=str(exc))
            return False
        return True


@contextmanager
def direct_connection(
    conninfo: str, *, autocommit: bool = False
) -> Iterator[psycopg.Connection[TupleRow]]:
    """A single short-lived connection for CLI jobs (migrations, ingestion)."""
    with psycopg.connect(
        conninfo,
        autocommit=autocommit,
        connect_timeout=_CONNECT_TIMEOUT_S,
        application_name=_APPLICATION_NAME,
    ) as conn:
        yield conn
