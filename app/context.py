"""请求级上下文：让入口生成的请求标识在整条调用链与异常处理器中可回溯。"""

from __future__ import annotations

import contextvars

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)


def set_request_id(value: str) -> contextvars.Token[str]:
    return request_id_var.set(value)


def get_request_id() -> str:
    return request_id_var.get()
