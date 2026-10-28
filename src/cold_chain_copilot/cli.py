"""`fde-copilot` command-line interface."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import psycopg
import typer
import uvicorn

from cold_chain_copilot import __version__
from cold_chain_copilot.core.config import Settings, get_settings
from cold_chain_copilot.core.exceptions import CopilotError
from cold_chain_copilot.core.logging import configure_logging, get_logger

app = typer.Typer(
    name="fde-copilot",
    help="Cold Chain Control Tower & AI Copilot.",
    no_args_is_help=True,
    add_completion=False,
)
logger = get_logger("cold_chain_copilot.cli")


def _settings() -> Settings:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    return settings


def _fail(message: str, code: int = 1) -> typer.Exit:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    return typer.Exit(code)


@app.command()
def version() -> None:
    """Print the package version."""
    typer.echo(__version__)


@app.command()
def serve(
    host: Annotated[str | None, typer.Option(help="Bind address (default: $HOST).")] = None,
    port: Annotated[int | None, typer.Option(help="Bind port (default: $PORT).")] = None,
    reload: Annotated[bool, typer.Option(help="Auto-reload on code changes (dev only).")] = False,
) -> None:
    """Run the FastAPI backend and the built SPA on one port (single worker)."""
    settings = _settings()
    uvicorn.run(
        "cold_chain_copilot.api.app:create_app",
        factory=True,
        host=host or settings.host,
        port=port or settings.port,
        reload=reload,
        workers=1,
        proxy_headers=True,
        forwarded_allow_ips=settings.forwarded_allow_ips,
        log_config=None,
        server_header=False,
    )


@app.command("setup-rbac")
def setup_rbac() -> None:
    """Create/re-key usr_fde_ro and apply 001_security_setup.sql (idempotent)."""
    from cold_chain_copilot.db.rbac import apply_security_setup

    settings = _settings()
    try:
        apply_security_setup(settings)
    except CopilotError as exc:
        raise _fail(str(exc), code=2) from exc
    except psycopg.Error as exc:
        raise _fail(f"database error: {exc}") from exc
    typer.secho(
        "RBAC setup applied: fde_views, vw_active_fleet, audit log, usr_fde_ro.", fg="green"
    )


@app.command("ingest-db")
def ingest_db(
    csv_path: Annotated[
        Path | None, typer.Option("--csv", help="Seed CSV (default: data/raw/fleet_telemetry.csv).")
    ] = None,
    regenerate: Annotated[
        bool, typer.Option(help="Rewrite the CSV from the scenario generator first.")
    ] = False,
    refresh_timestamps: Annotated[
        bool, typer.Option(help="Shift pings so the freshest equals now.")
    ] = True,
) -> None:
    """Generate the 25-row seed CSV if missing and upsert it into the legacy table."""
    from cold_chain_copilot.ingestion.telemetry_loader import load_telemetry

    settings = _settings()
    try:
        count = load_telemetry(
            settings.conninfo("admin"),
            csv_path or settings.seed_csv_path,
            regenerate=regenerate,
            refresh_timestamps=refresh_timestamps,
        )
    except CopilotError as exc:
        raise _fail(str(exc), code=2) from exc
    except psycopg.Error as exc:
        raise _fail(f"database error: {exc}") from exc
    typer.secho(f"Upserted {count} shipments into public.legacy_fleet_shipments.", fg="green")


@app.command("ingest-sops")
def ingest_sops(
    policy_dir: Annotated[
        Path | None, typer.Option(help="Policy directory (default: data/policy).")
    ] = None,
    ignore_cache: Annotated[
        bool,
        typer.Option(
            "--ignore-cache",
            help="Ignore .ingest_cache.json and re-diff every file against Pinecone "
            "(still embeds only missing chunks).",
        ),
    ] = False,
) -> None:
    """Chunk, hash, embed, and upsert SOP documents into Pinecone (unchanged files skipped)."""
    from openai import OpenAIError
    from pinecone.exceptions import PineconeException

    from cold_chain_copilot.ingestion.sop_indexer import SopIndexer

    settings = _settings()
    try:
        report = SopIndexer.from_settings(settings).sync_directory(
            policy_dir or settings.policy_dir,
            use_cache=not ignore_cache,
            on_file=lambda path: typer.echo(f"indexing {path.name}"),
        )
    except CopilotError as exc:
        raise _fail(str(exc), code=2) from exc
    except (OpenAIError, PineconeException, TimeoutError) as exc:
        raise _fail(f"{exc.__class__.__name__}: {exc}") from exc
    stats = report.as_dict()
    typer.secho(
        "SOP index synced: " + ", ".join(f"{key}={value}" for key, value in stats.items()),
        fg="green",
    )


if __name__ == "__main__":
    app()
