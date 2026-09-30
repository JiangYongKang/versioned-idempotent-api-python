"""归一化错误分类。

所有对外错误都带一个稳定的机器可读 ``code``，调用方可以据此区分
版本问题、字段问题、幂等冲突、并发超限、超时、取消、依赖失败等。
内部异常（栈、内部消息）永远不会直接透出。
"""

from __future__ import annotations

from typing import Any


class ErrorCode:
    """对外错误码集合（稳定契约，不要随意改名）。"""

    # 版本治理
    VERSION_MISSING = "VERSION_MISSING"
    VERSION_UNSUPPORTED = "VERSION_UNSUPPORTED"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    # 字段治理
    FIELD_INVALID = "FIELD_INVALID"
    NOT_FOUND = "NOT_FOUND"
    # 幂等与并发治理
    IDEMPOTENCY_KEY_MISSING = "IDEMPOTENCY_KEY_MISSING"
    IDEMPOTENCY_REPLAY_CONFLICT = "IDEMPOTENCY_REPLAY_CONFLICT"
    CONCURRENCY_LIMIT = "CONCURRENCY_LIMIT"
    # 超时 / 取消 / 依赖 / 关停
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    SHUTDOWN = "SHUTDOWN"
    # 兜底
    INTERNAL_ERROR = "INTERNAL_ERROR"


# code -> 默认 HTTP 状态码
DEFAULT_STATUS: dict[str, int] = {
    ErrorCode.VERSION_MISSING: 400,
    ErrorCode.VERSION_UNSUPPORTED: 400,
    ErrorCode.VERSION_CONFLICT: 400,
    ErrorCode.FIELD_INVALID: 422,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.IDEMPOTENCY_KEY_MISSING: 400,
    ErrorCode.IDEMPOTENCY_REPLAY_CONFLICT: 409,
    ErrorCode.CONCURRENCY_LIMIT: 429,
    ErrorCode.TIMEOUT: 504,
    ErrorCode.CANCELLED: 499,
    ErrorCode.UPSTREAM_ERROR: 502,
    ErrorCode.SHUTDOWN: 503,
    ErrorCode.INTERNAL_ERROR: 500,
}


class AppError(Exception):
    """对外可暴露的归一化业务错误（不携带内部堆栈）。"""

    def __init__(self, code: str, message: str, *, reason: str = "",
                 http_status: int | None = None,
                 details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        # reason：人类可读、稳定且可区分的判定依据
        self.reason = reason or message
        self.http_status = (
            http_status if http_status is not None
            else DEFAULT_STATUS.get(code, 500)
        )
        self.details = details or {}

    def to_body(self) -> dict[str, Any]:
        """归一化错误响应体（不含内部异常信息）。"""
        body: dict[str, Any] = {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "reason": self.reason,
            },
        }
        if self.details:
            body["error"]["details"] = self.details
        return body


def version_missing() -> AppError:
    return AppError(
        ErrorCode.VERSION_MISSING,
        "API version is required",
        reason="neither path prefix (/v1|/v2) nor X-API-Version header "
               "specified the API version",
    )


def version_unsupported(version: str) -> AppError:
    return AppError(
        ErrorCode.VERSION_UNSUPPORTED,
        f"unsupported API version: {version!r}",
        reason=f"version {version!r} is not among supported versions; "
               "supported: v1, v2",
        details={"version": version, "supported": ["v1", "v2"]},
    )


def version_conflict(path_version: str, header_version: str) -> AppError:
    return AppError(
        ErrorCode.VERSION_CONFLICT,
        "API version conflict between path and header",
        reason=f"path resolved version {path_version!r} but X-API-Version "
               f"header gave {header_version!r}",
        details={"path_version": path_version,
                 "header_version": header_version},
    )


def field_invalid(field: str, problem: str, *, value: Any = None) -> AppError:
    return AppError(
        ErrorCode.FIELD_INVALID,
        f"invalid field: {field}",
        reason=f"field {field!r} {problem}",
        details={"field": field, "value": _safe_repr(value)},
    )


def idempotency_key_missing() -> AppError:
    return AppError(
        ErrorCode.IDEMPOTENCY_KEY_MISSING,
        "Idempotency-Key header is required",
        reason="mutating requests must carry an Idempotency-Key so retries "
               "and duplicate submissions apply side effects exactly once",
    )


def replay_conflict(key: str) -> AppError:
    return AppError(
        ErrorCode.IDEMPOTENCY_REPLAY_CONFLICT,
        "idempotency key was already used with a different request",
        reason=f"idempotency key {key!r} is bound to a different request "
               "fingerprint; a key may only be replayed with the same body",
        details={"idempotency_key": key},
    )


def concurrency_limit_reached(max_inflight: int) -> AppError:
    return AppError(
        ErrorCode.CONCURRENCY_LIMIT,
        "server is handling too many in-flight requests",
        reason=f"global in-flight limit of {max_inflight} reached; the "
               "request is rejected immediately rather than queued",
        details={"max_inflight": max_inflight},
        http_status=429,
    )


def timeout_error(stage: str, timeout: float) -> AppError:
    return AppError(
        ErrorCode.TIMEOUT,
        f"request timed out at stage {stage!r}",
        reason=f"stage {stage!r} did not finish within {timeout:.3f}s "
               "budget; no partial write was committed",
        details={"stage": stage, "timeout_seconds": timeout},
    )


def cancelled_error(stage: str) -> AppError:
    return AppError(
        ErrorCode.CANCELLED,
        f"request cancelled at stage {stage!r}",
        reason=f"the caller (or server shutdown) cancelled the request at "
               f"stage {stage!r} before it committed; no partial write kept",
        details={"stage": stage},
    )


def upstream_error(dependency: str, detail: str) -> AppError:
    return AppError(
        ErrorCode.UPSTREAM_ERROR,
        f"upstream dependency {dependency!r} failed",
        # 只透出依赖名与受控的失败类别，不透出内部异常文本
        reason=f"dependency {dependency!r} returned a failure: {detail}",
        details={"dependency": dependency},
    )


def shutdown_error() -> AppError:
    return AppError(
        ErrorCode.SHUTDOWN,
        "service is shutting down",
        reason="shutdown signal received; new work is rejected and this "
               "request could not be completed deterministically",
    )


def not_found(resource: str, resource_id: str) -> AppError:
    return AppError(
        ErrorCode.NOT_FOUND,
        f"{resource} not found: {resource_id}",
        reason=f"no committed {resource} exists for id {resource_id!r}",
        details={"resource": resource, "id": resource_id},
    )


def internal_error() -> AppError:
    # 固定文案：绝不回传内部异常细节
    return AppError(
        ErrorCode.INTERNAL_ERROR,
        "internal server error",
        reason="an unexpected internal condition occurred; it has been "
               "logged with the request id",
    )


def _safe_repr(value: Any) -> str:
    try:
        return repr(value)
    except Exception:  # pragma: no cover - 极端对象的防御
        return "<unreprable>"
