"""对外归一化的错误类型。

任何内部异常（超时 / 取消 / 外部依赖失败 / 校验失败）都必须在边界处
转换成 :class:`ServiceError`，禁止把原生异常直接透出给调用方。
"""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    """可区分的错误原因枚举。"""

    VERSION_MISSING = "version_missing"
    VERSION_UNSUPPORTED = "version_unsupported"
    INVALID_FIELD = "invalid_field"
    IDEMPOTENCY_KEY_MISSING = "idempotency_key_missing"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    CONCURRENCY_LIMIT_EXCEEDED = "concurrency_limit_exceeded"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    EXTERNAL_DEPENDENCY_FAILED = "external_dependency_failed"
    SHUTTING_DOWN = "shutting_down"
    INTERNAL_ERROR = "internal_error"


class ServiceError(Exception):
    """所有对外错误的唯一基类。"""

    code: ErrorCode = ErrorCode.INTERNAL_ERROR
    message: str = "internal error"
    retryable: bool = False

    def __init__(self, message: str | None = None, *, request_id: str | None = None):
        self.request_id = request_id
        if message is not None:
            self.message = message
        super().__init__(f"[{self.code.value}] {self.message}")

    def to_dict(self) -> dict:
        return {
            "error": {
                "code": self.code.value,
                "message": self.message,
                "retryable": self.retryable,
                "request_id": self.request_id,
            }
        }


class VersionMissingError(ServiceError):
    code = ErrorCode.VERSION_MISSING
    message = "API version is required"
    retryable = False


class VersionUnsupportedError(ServiceError):
    code = ErrorCode.VERSION_UNSUPPORTED
    message = "unsupported API version"
    retryable = False


class InvalidFieldError(ServiceError):
    code = ErrorCode.INVALID_FIELD
    message = "invalid field"
    retryable = False


class IdempotencyKeyMissingError(ServiceError):
    code = ErrorCode.IDEMPOTENCY_KEY_MISSING
    message = "idempotency key is required"
    retryable = False


class IdempotencyConflictError(ServiceError):
    """同键但请求体不同。"""

    code = ErrorCode.IDEMPOTENCY_CONFLICT
    message = "idempotency key reused with a different request payload"
    retryable = False


class ConcurrencyLimitExceededError(ServiceError):
    code = ErrorCode.CONCURRENCY_LIMIT_EXCEEDED
    message = "in-flight request limit exceeded"
    retryable = True


class TimeoutError(ServiceError):
    code = ErrorCode.TIMEOUT
    message = "request timed out"
    retryable = True


class CancelledError(ServiceError):
    code = ErrorCode.CANCELLED
    message = "request cancelled"
    retryable = True


class ExternalDependencyError(ServiceError):
    code = ErrorCode.EXTERNAL_DEPENDENCY_FAILED
    message = "external dependency failed"
    retryable = True


class ShuttingDownError(ServiceError):
    code = ErrorCode.SHUTTING_DOWN
    message = "service is shutting down"
    retryable = False


class InternalError(ServiceError):
    """内部异常归一化后的对外类型（不泄露任何内部细节）。"""

    code = ErrorCode.INTERNAL_ERROR
    message = "internal error"
    retryable = True
