"""结构化判定日志：每个关键判定点打印请求标识、幂等键与依据。"""

from __future__ import annotations

import logging
import sys

_LOGGER_NAME = "versioned_idempotent_api"


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False
    if not any(getattr(h, "_via_bootstrap", False) for h in logger.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler._via_bootstrap = True  # type: ignore[attr-defined]
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s [request_id=%(request_id)s "
            "idem_key=%(idem_key)s] %(decision)s | %(message)s"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger


def get_logger() -> logging.Logger:
    logger = logging.getLogger(_LOGGER_NAME)
    if not logger.handlers:
        return configure_logging()
    return logger


def log_decision(
    logger: logging.Logger,
    *,
    request_id: str,
    idempotency_key: str | None,
    decision: str,
    reason: str,
    level: int = logging.INFO,
    **extra: object,
) -> None:
    """统一判定日志格式。

    必含字段：请求标识、幂等键、判定结论、判定依据；
    其余上下文以 key=value 拼接到消息尾部。
    """
    level = int(extra.pop("level", level))
    suffix = " ".join(f"{k}={v}" for k, v in extra.items())
    message = f"reason={reason}" + (f" {suffix}" if suffix else "")
    logger.log(
        level,
        message,
        extra={
            "request_id": request_id,
            "idem_key": idempotency_key or "-",
            "decision": decision,
        },
    )
