"""Domain exceptions raised across the copilot."""

from __future__ import annotations


class CopilotError(Exception):
    """Base class for every error the copilot raises deliberately."""


class ConfigurationError(CopilotError):
    """Required settings are missing or inconsistent."""


class SQLGuardError(CopilotError):
    """A query was rejected by the read-only SQL guard before reaching the database."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class RBACViolationError(CopilotError):
    """PostgreSQL refused a statement because the agent role lacks the privilege."""

    def __init__(self, message: str, *, sqlstate: str | None = None) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


class SQLRetryExhaustedError(CopilotError):
    """The Pro model could not repair a failing query within the allowed attempts."""

    def __init__(self, attempts: int, last_error: str) -> None:
        super().__init__(
            f"SQL self-correction gave up after {attempts} attempt(s). Last error: {last_error}"
        )
        self.attempts = attempts
        self.last_error = last_error


class ToolExecutionError(CopilotError):
    """An external dependency behind a tool (weather, vector search) failed."""
