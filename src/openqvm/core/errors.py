"""Structured error types used across OpenQVM.

Every user-facing failure carries a stable machine-readable code, a message that is
safe to display, an optional remediation hint and optional structured details.
Backends must never report success when the underlying operation failed; they raise
one of these errors instead.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    VALIDATION_FAILED = "validation_failed"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    INVALID_STATE = "invalid_state"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    BACKEND_ERROR = "backend_error"
    OPERATION_FAILED = "operation_failed"
    PERMISSION_DENIED = "permission_denied"
    CORRUPT_DATA = "corrupt_data"
    UNSUPPORTED = "unsupported"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    IO_ERROR = "io_error"
    SECURITY = "security"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True)
class ValidationIssue:
    """A single problem (or warning) found while validating user input."""

    field: str
    message: str
    severity: str = "error"  # "error" | "warning"

    @property
    def is_error(self) -> bool:
        return self.severity == "error"

    def as_text(self) -> str:
        if not self.field:
            return self.message
        return f"{self.field}: {self.message}"

    def to_dict(self) -> dict[str, str]:
        return {"field": self.field, "message": self.message, "severity": self.severity}


class OpenQVMError(Exception):
    """Base class for all expected OpenQVM failures."""

    code: ErrorCode = ErrorCode.OPERATION_FAILED

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode | None = None,
        hint: str | None = None,
        details: dict[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.hint = hint
        self.details: dict[str, Any] = dict(details or {})
        if cause is not None:
            self.__cause__ = cause

    def __str__(self) -> str:
        return self.message

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code.value,
            "message": self.message,
        }
        if self.hint:
            payload["hint"] = self.hint
        if self.details:
            payload["details"] = self.details
        return payload

    def format_for_user(self, *, include_details: bool = False) -> str:
        text = self.message
        if self.hint:
            text = f"{text}\n\n{self.hint}"
        if include_details and self.details:
            details = ", ".join(f"{key}={value}" for key, value in self.details.items())
            text = f"{text}\n\nDetails: {details}"
        return text


class ValidationError(OpenQVMError):
    code = ErrorCode.VALIDATION_FAILED

    def __init__(
        self,
        message: str = "The configuration is not valid.",
        *,
        issues: Iterable[ValidationIssue] = (),
        hint: str | None = None,
        details: dict[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        self.issues: list[ValidationIssue] = list(issues)
        payload = dict(details or {})
        if self.issues:
            payload.setdefault("issues", [issue.to_dict() for issue in self.issues])
        super().__init__(message, hint=hint, details=payload, cause=cause)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.is_error]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.severity == "warning"]


class NotFoundError(OpenQVMError):
    code = ErrorCode.NOT_FOUND


class ConflictError(OpenQVMError):
    code = ErrorCode.CONFLICT


class InvalidStateError(OpenQVMError):
    code = ErrorCode.INVALID_STATE


class BackendUnavailableError(OpenQVMError):
    code = ErrorCode.BACKEND_UNAVAILABLE


class BackendError(OpenQVMError):
    code = ErrorCode.BACKEND_ERROR


class OperationFailedError(OpenQVMError):
    code = ErrorCode.OPERATION_FAILED


class PermissionDeniedError(OpenQVMError):
    code = ErrorCode.PERMISSION_DENIED


class CorruptDataError(OpenQVMError):
    code = ErrorCode.CORRUPT_DATA


class UnsupportedFeatureError(OpenQVMError):
    code = ErrorCode.UNSUPPORTED


class TimeoutError_(OpenQVMError):
    code = ErrorCode.TIMEOUT


class CancelledError(OpenQVMError):
    code = ErrorCode.CANCELLED


class SecurityError(OpenQVMError):
    code = ErrorCode.SECURITY


def raise_if_errors(issues: Iterable[ValidationIssue], *, message: str = "The configuration is not valid.") -> None:
    """Raise :class:`ValidationError` when *issues* contains at least one error."""

    collected = list(issues)
    if any(issue.is_error for issue in collected):
        summary = "; ".join(issue.as_text() for issue in collected if issue.is_error)
        raise ValidationError(f"{message} {summary}".strip(), issues=collected)


def error_from_exception(exc: BaseException, *, default_message: str) -> OpenQVMError:
    """Normalize an arbitrary exception into a user-facing :class:`OpenQVMError`."""

    if isinstance(exc, OpenQVMError):
        return exc
    if isinstance(exc, PermissionError):
        return PermissionDeniedError(
            default_message,
            hint="Check file ownership and permissions, then try again.",
            cause=exc,
        )
    if isinstance(exc, FileNotFoundError):
        return OperationFailedError(default_message, hint="A required file or tool is missing.", cause=exc)
    return OperationFailedError(default_message, cause=exc)


@dataclass
class ErrorReport:
    """Serializable error payload for CLI/JSON output."""

    code: str
    message: str
    hint: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_error(cls, error: OpenQVMError) -> ErrorReport:
        return cls(
            code=error.code.value,
            message=error.message,
            hint=error.hint,
            details=error.details,
        )
