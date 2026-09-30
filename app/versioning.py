"""版本协商：从入口信息解析受支持的 API 版本。"""

from __future__ import annotations

from enum import Enum

from app.errors import VersionMissingError, VersionUnsupportedError


class ApiVersion(str, Enum):
    V1 = "v1"
    V2 = "v2"


SUPPORTED_VERSIONS = frozenset(v.value for v in ApiVersion)


def resolve_version(raw: str | None) -> ApiVersion:
    """解析版本标识。

    - 为空 / 全空白 -> VersionMissingError（区别于“传了但不认识”）
    - 去掉前后空白、大小写不敏感；不在支持列表 -> VersionUnsupportedError
    """
    if raw is None or not raw.strip():
        raise VersionMissingError("request header X-Api-Version is required")
    candidate = raw.strip().lower()
    for v in ApiVersion:
        if candidate == v.value:
            return v
    raise VersionUnsupportedError(
        f"API version {raw.strip()!r} is not supported; "
        f"supported: {sorted(SUPPORTED_VERSIONS)}"
    )
