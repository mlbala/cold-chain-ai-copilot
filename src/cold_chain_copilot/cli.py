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


_TOOL_SHORT = {"query_fleet_telemetry": "sql", "get_live_weather": "weather", "search_sop": "sop"}
_STATUS_STYLE = {
    "passed": ("PASS", typer.colors.GREEN),
    "failed": ("FAIL", typer.colors.RED),
    "error": ("ERR ", typer.colors.RED),
    "skipped": ("SKIP", typer.colors.YELLOW),
}


@app.command("eval")
def eval_copilot(
    cases_path: Annotated[Path, typer.Option("--cases", help="Case file.")] = Path(
        "evals/copilot_cases.toml"
    ),
    only: Annotated[
        list[str] | None, typer.Option("--only", help="Run only this case id (repeatable).")
    ] = None,
    max_total_cost: Annotated[
        float, typer.Option(min=0.01, help="Stop starting new cases once this much USD is spent.")
    ] = 0.50,
    report_dir: Annotated[Path, typer.Option(help="Where the JSON report is written.")] = Path(
        "evals/reports"
    ),
    baseline: Annotated[
        Path | None, typer.Option(help="Earlier JSON report to compare against.")
    ] = None,
    verbose: Annotated[bool, typer.Option(help="Show agent logs while cases run.")] = False,
) -> None:
    """Run the live copilot test set (real model, database, weather, and Pinecone calls)."""
    from cold_chain_copilot.evals.cases import load_suite
    from cold_chain_copilot.evals.runner import (
        read_report,
        require_eval_config,
        run_suite,
        write_report,
    )
    from cold_chain_copilot.evals.scoring import CaseResult, compare_reports

    settings = get_settings()
    configure_logging(settings.log_level if verbose else "WARNING", settings.log_format)
    try:
        suite = load_suite(cases_path)
        cases = suite.select(only or [])
        previous = read_report(baseline) if baseline else None
        require_eval_config(settings)
    except (OSError, ValueError) as exc:  # ValidationError and TOMLDecodeError are ValueErrors
        raise _fail(f"{exc.__class__.__name__}: {exc}", code=2) from exc
    except CopilotError as exc:
        raise _fail(str(exc), code=2) from exc

    def show(result: CaseResult) -> None:
        label, color = _STATUS_STYLE[result.status]
        tools = ", ".join(_TOOL_SHORT.get(name, name) for name in result.tools_used) or "-"
        models = " → ".join(result.models_used) or "-"
        typer.secho(f"{label}  ", fg=color, nl=False)
        typer.echo(
            f"{result.case_id:<28} {tools:<20} {models:<24} "
            f"${result.cost_usd:.4f}  {result.latency_ms / 1000:5.1f} s"
        )

    typer.echo(f"Running {len(cases)} case(s) from {cases_path} …")
    report = run_suite(settings, suite, cases, max_total_cost_usd=max_total_cost, on_result=show)
    path = write_report(report, report_dir)

    problems = [result for result in report.results if result.status != "passed"]
    if problems:
        typer.echo("")
        for result in problems:
            typer.secho(f"{result.case_id}: {result.question}", bold=True)
            if result.error:
                typer.echo(f"  - {result.error}")
            for check in result.failed_checks:
                typer.echo(f"  - {check.name}: {check.detail}")
    if previous is not None:
        typer.echo("")
        for line in compare_reports(report, previous) or ["no changes against the baseline"]:
            typer.secho(line, fg=typer.colors.RED if line.startswith("REGRESSION") else None)

    latencies = sorted(result.latency_ms for result in report.results if result.latency_ms)
    median_s = latencies[len(latencies) // 2] / 1000 if latencies else 0.0
    typer.echo("")
    typer.secho(
        f"{report.passed}/{len(report.results)} passed · total ${report.total_cost_usd:.4f} · "
        f"median {median_s:.1f} s · report {path}",
        fg=typer.colors.GREEN if report.ok else typer.colors.RED,
        bold=True,
    )
    if not report.ok:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
