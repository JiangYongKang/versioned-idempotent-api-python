"""接口版本解析、协商与校验。

版本来源（任一即可，同时给出时必须一致）：
1. URL 路径前缀：``/v1/orders``、``/v2/orders``
2. 请求头：``X-API-Version: v1|v2``

无版本前缀的通用入口 ``/orders`` 上，头也没给 -> VERSION_MISSING。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from . import errors as err
from .models import validate_and_normalize

SUPPORTED_VERSIONS: frozenset[str] = frozenset({"v1", "v2"})
DEFAULT_VERSION: str = "v1"
_HEADER = "x-api-version"


def header_name() -> str:
    return _HEADER


def resolve_version(path_version: str | None,
                    header_version: str | None) -> str:
    """从路径前缀与 X-API-Version 头解析最终版本。

    - 两处都缺省 -> VERSION_MISSING
    - 给出版本不支持 -> VERSION_UNSUPPORTED
    - 两处都给但不一致 -> VERSION_CONFLICT
    """
    path_version = _normalize(path_version)
    header_version = _normalize(header_version)

    if path_version is None and header_version is None:
        raise err.version_missing()
    if path_version is not None and path_version not in SUPPORTED_VERSIONS:
        raise err.version_unsupported(path_version)
    if header_version is not None and header_version not in SUPPORTED_VERSIONS:
        raise err.version_unsupported(header_version)
    chosen = path_version or header_version
    if (path_version is not None and header_version is not None
            and path_version != header_version):
        raise err.version_conflict(path_version, header_version)
    return chosen  # type: ignore[return-value]


def parse_body(raw: bytes | str | dict[str, Any] | None) -> dict[str, Any]:
    """把原始请求体解析为 dict；体为空或 JSON 非法归为 FIELD_INVALID。"""
    if raw is None or raw == "":
        raise err.field_invalid("body", "request body is required")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise err.field_invalid("body", "must be a JSON object") from None
    if not isinstance(parsed, dict):
        raise err.field_invalid("body", "must be a JSON object")
    return parsed


def validate_payload(version: str, raw: dict[str, Any]) -> dict[str, Any]:
    """按版本校验并解析请求体，字段非法抛出 FIELD_INVALID。"""
    try:
        return validate_and_normalize(version, raw)
    except ValidationError as exc:
        # pydantic 可能一次报多个错，取第一个作为可区分原因
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first["loc"]) or "body"
        problem = _humanize(first)
        raise err.field_invalid(loc, problem, value=first.get("input")) from None


def _normalize(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip().lower()
    return value or None


def _humanize(error: dict[str, Any]) -> str:
    etype = error.get("type", "")
    if etype in ("missing", "value_error.missing"):
        return "is required"
    if etype.startswith("greater_than_equal") or etype == "ge":
        return f"violates constraint: {error.get('msg', 'invalid')}"
    if etype in ("string_too_short", "string_too_long"):
        return f"violates length constraint: {error.get('msg', 'invalid')}"
    if etype in ("enum", "literal_error"):
        return "is not one of the allowed values"
    return str(error.get("msg", "is invalid"))
