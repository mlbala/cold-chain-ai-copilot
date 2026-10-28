"""FastAPI application factory: lifespan-managed services, API routes, and the SPA."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import Scope

from cold_chain_copilot import __version__
from cold_chain_copilot.agent.graph import build_copilot
from cold_chain_copilot.api.dependencies import AppRuntime, ChatGuards, RuntimeDep
from cold_chain_copilot.api.schemas import HealthResponse
from cold_chain_copilot.api.v1 import api_router
from cold_chain_copilot.core.config import Settings, get_settings
from cold_chain_copilot.core.exceptions import ConfigurationError
from cold_chain_copilot.core.logging import configure_logging, get_logger
from cold_chain_copilot.core.metrics import metrics
from cold_chain_copilot.db.audit import AuditLogger
from cold_chain_copilot.db.pool import DatabasePools
from cold_chain_copilot.ingestion.telemetry_simulator import TelemetrySimulator

logger = get_logger(__name__)

_CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; connect-src 'self'; font-src 'self' data:; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)


class SPAStaticFiles(StaticFiles):
    """Serves the built SPA and falls back to index.html for client-side routes."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404 or path.startswith("api/"):
                raise
            return await super().get_response("index.html", scope)


async def _security_headers(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if not request.url.path.startswith("/api/docs"):
        response.headers.setdefault("Content-Security-Policy", _CSP)
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.require_serving_config()
        metrics.reset()
        pools = DatabasePools.from_settings(settings)
        pools.open()
        audit = AuditLogger(pools.ro)
        audit.start()
        simulator: TelemetrySimulator | None = None
        try:
            if settings.telemetry_simulator:
                simulator = TelemetrySimulator(
                    pools.admin, interval_s=settings.telemetry_simulator_interval_seconds
                )
                simulator.start()
            copilot = build_copilot(settings, pools.ro, audit)
            app.state.runtime = AppRuntime(
                settings=settings,
                pools=pools,
                audit=audit,
                copilot=copilot,
                guards=ChatGuards.from_settings(settings),
                simulator=simulator,
            )
            logger.info(
                "startup_complete",
                version=__version__,
                env=settings.app_env,
                flash_model=settings.deepseek_flash_model,
                pro_model=settings.deepseek_pro_model,
            )
            yield
        finally:
            app.state.runtime = None
            if simulator is not None:
                simulator.stop()
            audit.stop()
            pools.close()
            logger.info("shutdown_complete")

    app = FastAPI(
        title="Cold Chain Control Tower API",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.middleware("http")(_security_headers)

    @app.exception_handler(ConfigurationError)
    async def _configuration_error(request: Request, exc: ConfigurationError) -> JSONResponse:
        logger.error("configuration_error", error=str(exc), path=request.url.path)
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.get("/api/v1/health", response_model=HealthResponse, tags=["ops"])
    def health(runtime: RuntimeDep) -> HealthResponse:
        database_ok = runtime.pools.ping("ro", timeout=2.0)
        return HealthResponse(
            status="ok" if database_ok else "degraded",
            version=__version__,
            environment=runtime.settings.app_env,
            database="ok" if database_ok else "unavailable",
        )

    app.include_router(api_router, prefix="/api/v1")
    _mount_spa(app, settings.frontend_dist)
    return app


def _mount_spa(app: FastAPI, dist: Path) -> None:
    index = dist / "index.html"
    if not index.is_file():
        logger.warning("spa_not_built", path=str(dist), hint="run `make frontend-build`")
        return
    app.mount("/", SPAStaticFiles(directory=dist, html=True), name="spa")
