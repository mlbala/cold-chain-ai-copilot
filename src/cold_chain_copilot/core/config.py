"""Typed application settings loaded from the environment and `.env`."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from cold_chain_copilot.core.exceptions import ConfigurationError

AGENT_ROLE_NAME = "usr_fde_ro"

DbRole = Literal["admin", "agent"]

SecretName = Literal[
    "aiven_admin_db_uri",
    "aiven_agent_ro_db_uri",
    "aiven_admin_password",
    "aiven_agent_ro_password",
    "deepseek_api_key",
    "openai_api_key",
    "pinecone_api_key",
    "admin_audit_password",
]

SERVING_SECRETS: tuple[SecretName, ...] = (
    "deepseek_api_key",
    "openai_api_key",
    "pinecone_api_key",
    "admin_audit_password",
)


class Settings(BaseSettings):
    """Every tunable of the service. Secrets stay wrapped in `SecretStr` until used."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: Literal["development", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["console", "json"] = "console"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)

    # Aiven PostgreSQL. Preferred: separate fields (used whenever AIVEN_HOST is set).
    aiven_host: str | None = None
    aiven_port: int | None = Field(default=None, ge=1, le=65535)
    aiven_db_name: str = "defaultdb"
    aiven_sslmode: Literal["require", "verify-ca", "verify-full"] = "require"
    aiven_admin_user: str = "avnadmin"
    aiven_admin_password: SecretStr | None = None
    aiven_agent_ro_user: str = AGENT_ROLE_NAME
    aiven_agent_ro_password: SecretStr | None = None
    # Alternative: full service URIs (used only when AIVEN_HOST is not set).
    aiven_admin_db_uri: SecretStr | None = None
    aiven_agent_ro_db_uri: SecretStr | None = None
    db_pool_ro_max_size: int = Field(default=4, ge=1, le=20)
    db_pool_admin_max_size: int = Field(default=2, ge=1, le=10)

    deepseek_api_key: SecretStr | None = None
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_flash_model: str = "deepseek-chat"
    deepseek_pro_model: str = "deepseek-reasoner"
    flash_input_price_per_m: float = Field(default=0.15, ge=0)
    pro_input_price_per_m: float = Field(default=0.66, ge=0)
    flash_output_price_per_m: float = Field(default=0.0, ge=0)
    pro_output_price_per_m: float = Field(default=0.0, ge=0)
    # DeepSeek prefix-cache hits; unset means "bill hits at the full input price".
    flash_cached_input_price_per_m: float | None = Field(default=None, ge=0)
    pro_cached_input_price_per_m: float | None = Field(default=None, ge=0)
    llm_timeout_seconds: float = Field(default=90.0, gt=0)

    openai_api_key: SecretStr | None = None
    openai_embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = Field(default=1536, gt=0)
    pinecone_api_key: SecretStr | None = None
    pinecone_index_name: str = "fde-sop-index-openai"
    pinecone_cloud: str = "aws"
    pinecone_region: str = "us-east-1"
    pinecone_namespace: str = "sops"
    sop_top_k: int = Field(default=4, ge=1, le=10)

    open_meteo_base_url: str = "https://api.open-meteo.com/v1/forecast"
    weather_timeout_seconds: float = Field(default=8.0, gt=0)

    admin_audit_password: SecretStr | None = None

    sql_max_fix_attempts: int = Field(default=2, ge=0, le=5)
    sql_row_limit: int = Field(default=200, ge=1, le=1000)
    sql_statement_timeout_ms: int = Field(default=5000, ge=100, le=60000)
    agent_max_tool_calls: int = Field(default=10, ge=1, le=20)
    history_turns: int = Field(default=4, ge=1, le=20)

    # API guardrails
    chat_rate_limit_per_minute: int = Field(default=12, ge=1, le=600)
    chat_max_concurrent: int = Field(default=4, ge=1, le=32)
    llm_daily_budget_usd: float = Field(default=5.0, ge=0)  # 0 disables the cap
    forwarded_allow_ips: str = "127.0.0.1"  # proxies trusted for X-Forwarded-For
    health_probe_ttl_seconds: float = Field(default=60.0, ge=5, le=3600)

    data_dir: Path = Path("data")
    frontend_dist: Path = Path("frontend/dist")

    def secret(self, name: SecretName) -> str:
        """Return a required secret's plain value or raise `ConfigurationError`."""
        value: SecretStr | None = getattr(self, name)
        if value is None or not value.get_secret_value().strip():
            raise ConfigurationError(
                f"{name.upper()} is not set. Add it to .env or the environment."
            )
        return value.get_secret_value()

    def missing_secrets(self, names: tuple[SecretName, ...] = SERVING_SECRETS) -> list[str]:
        """Names (as env vars) of required secrets that are absent or blank."""
        missing: list[str] = []
        for name in names:
            value: SecretStr | None = getattr(self, name)
            if value is None or not value.get_secret_value().strip():
                missing.append(name.upper())
        return missing

    def missing_db_settings(self) -> list[str]:
        """Env vars still needed to connect as both database roles."""
        if self.aiven_host:
            required: tuple[SecretName, ...] = ("aiven_admin_password", "aiven_agent_ro_password")
            missing = self.missing_secrets(required)
            return ["AIVEN_PORT", *missing] if self.aiven_port is None else missing
        return self.missing_secrets(("aiven_admin_db_uri", "aiven_agent_ro_db_uri"))

    def require_serving_config(self) -> None:
        """Fail fast at startup when the API cannot serve every endpoint."""
        missing = self.missing_db_settings() + self.missing_secrets()
        if missing:
            raise ConfigurationError("Missing required settings: " + ", ".join(missing))
        self.agent_role_credentials()

    def db_params(self, role: DbRole) -> dict[str, str]:
        """Connection parameters for one role.

        With `AIVEN_HOST` set they come from the separate AIVEN_* fields; otherwise they are
        parsed out of `AIVEN_ADMIN_DB_URI` / `AIVEN_AGENT_RO_DB_URI`.
        """
        if self.aiven_host:
            if self.aiven_port is None:
                raise ConfigurationError("AIVEN_PORT is required when AIVEN_HOST is set.")
            admin = role == "admin"
            return {
                "host": self.aiven_host,
                "port": str(self.aiven_port),
                "dbname": self.aiven_db_name,
                "user": self.aiven_admin_user if admin else self.aiven_agent_ro_user,
                "password": self.secret(
                    "aiven_admin_password" if admin else "aiven_agent_ro_password"
                ),
                "sslmode": self.aiven_sslmode,
            }
        uri = self.secret("aiven_admin_db_uri" if role == "admin" else "aiven_agent_ro_db_uri")
        return {key: str(value) for key, value in conninfo_to_dict(uri).items()}

    def conninfo(self, role: DbRole) -> str:
        """libpq connection string for `role`, with every value safely quoted."""
        return make_conninfo(**self.db_params(role))

    def agent_role_credentials(self) -> tuple[str, str]:
        """Username and password for the agent role, validated for least privilege.

        The username must be the role created by `setup-rbac`.
        """
        params = self.db_params("agent")
        username = params.get("user", "")
        password = params.get("password", "")
        source = "AIVEN_AGENT_RO_USER" if self.aiven_host else "AIVEN_AGENT_RO_DB_URI"
        if username != AGENT_ROLE_NAME:
            raise ConfigurationError(
                f"{source} must be {AGENT_ROLE_NAME!r}, got {username or 'no user'!r}."
            )
        if len(password) < 12:
            raise ConfigurationError(
                "The agent role password must be at least 12 characters "
                f"({'AIVEN_AGENT_RO_PASSWORD' if self.aiven_host else 'AIVEN_AGENT_RO_DB_URI'})."
            )
        return username, password

    @property
    def seed_csv_path(self) -> Path:
        return self.data_dir / "raw" / "fleet_telemetry.csv"

    @property
    def policy_dir(self) -> Path:
        return self.data_dir / "policy"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()
