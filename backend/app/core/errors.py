"""RFC 7807 problem-details error model.

`remediation` is what lets the Copilot offer the cross-department request flow
instead of returning a dead end to the user.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

ERROR_BASE = "https://bidready/errors/"


class AppError(Exception):
    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    error_type: str = "internal_error"
    title: str = "Internal error"

    def __init__(
        self,
        detail: str,
        *,
        remediation: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.remediation = remediation
        self.extra = extra or {}


class ValidationError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    error_type = "validation_error"
    title = "Validation error"


class AuthenticationError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    error_type = "authentication_required"
    title = "Authentication required"


class PermissionDenied(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    error_type = "permission_denied"
    title = "Permission denied"


class NotFound(AppError):
    """404 is also returned for rows outside the caller's tenant.

    The two cases are deliberately indistinguishable: telling a caller that a
    resource exists but belongs to someone else is itself a disclosure.
    """

    status_code = status.HTTP_404_NOT_FOUND
    error_type = "not_found"
    title = "Not found"


class StateConflict(AppError):
    status_code = status.HTTP_409_CONFLICT
    error_type = "state_conflict"
    title = "State conflict"


class IllegalTransition(StateConflict):
    error_type = "illegal_transition"
    title = "Illegal state transition"


class ProviderError(AppError):
    status_code = status.HTTP_502_BAD_GATEWAY
    error_type = "provider_error"
    title = "Upstream model provider error"


def problem_response(
    request: Request,
    *,
    status_code: int,
    error_type: str,
    title: str,
    detail: str,
    remediation: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"{ERROR_BASE}{error_type}",
        "title": title,
        "status": status_code,
        "detail": detail,
        "instance": str(request.url.path),
        "request_id": getattr(request.state, "request_id", None),
    }
    if remediation:
        body["remediation"] = remediation
    if extra:
        body.update(extra)
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(body),
        media_type="application/problem+json",
    )


def install_error_handlers(app) -> None:
    from app.core.logging import get_logger

    log = get_logger("errors")

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError):
        if exc.status_code >= 500:
            log.error("app_error", error_type=exc.error_type, detail=exc.detail)
        return problem_response(
            request,
            status_code=exc.status_code,
            error_type=exc.error_type,
            title=exc.title,
            detail=exc.detail,
            remediation=exc.remediation,
            extra=exc.extra,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        return problem_response(
            request,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            error_type="schema_validation_error",
            title="Request does not match the schema",
            detail="One or more fields failed validation.",
            extra={"errors": exc.errors()},
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        log.exception("unhandled_exception")
        return problem_response(
            request,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_type="internal_error",
            title="Internal error",
            detail="An unexpected error occurred.",
        )
