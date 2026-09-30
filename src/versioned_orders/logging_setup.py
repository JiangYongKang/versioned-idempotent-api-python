"""带请求标识 / 幂等键 / 判定依据的结构化日志。

单测要求日志里能看到：请求标识（request_id）、幂等键（idempotency_key）
以及判定依据（decision / reason）。所有关键判定都走 :func:`log_decision`。
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

_CONFIGURED = False


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "gov", None)
        if isinstance(extra, dict):
            payload.update(extra)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def configure_logging(level: int = logging.INFO) -> None:
    """配置全局日志（幂等，可重复调用）。"""
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger("versioned_orders")
    root.handlers[:] = [handler]
    root.setLevel(level)
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """获取包内 logger，未配置时自动用默认配置初始化。"""
    configure_logging()
    if name.startswith("versioned_orders"):
        return logging.getLogger(name)
    return logging.getLogger(f"versioned_orders.{name}")


def log_decision(logger: logging.Logger, *, request_id: str,
                 idempotency_key: str | None, decision: str,
                 reason: str = "", level: int = logging.INFO,
                 **extra: Any) -> None:
    """打印一条判定日志：请求标识、幂等键与判定依据。

    decision 用稳定的短标识，例如：
    version_resolved / field_invalid / idem_first / idem_replay /
    idem_inflight_wait / idem_conflict / concurrency_rejected /
    timeout / cancelled / upstream_failed / committed / shutdown_reject
    """
    gov: dict[str, Any] = {
        "request_id": request_id,
        "idempotency_key": idempotency_key or "-",
        "decision": decision,
        "reason": reason,
    }
    gov.update(extra)
    logger.log(level, "gov-decision", extra={"gov": gov})
