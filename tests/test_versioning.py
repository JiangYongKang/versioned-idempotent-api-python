"""版本协商边界：缺失 / 不支持 / 字段非法 / 新旧版本默认值与返回结构。"""

from __future__ import annotations

import pytest

from app.errors import (
    IdempotencyConflictError,
    InvalidFieldError,
    VersionMissingError,
    VersionUnsupportedError,
)
from app.models import build_response
from app.versioning import ApiVersion, resolve_version


def test_resolve_version_ok() -> None:
    assert resolve_version("v1") is ApiVersion.V1
    assert resolve_version("v2") is ApiVersion.V2
    # 大小写 / 空白容错
    assert resolve_version(" V2 ") is ApiVersion.V2


def test_resolve_version_missing() -> None:
    with pytest.raises(VersionMissingError) as ei:
        resolve_version(None)
    assert ei.value.code.value == "version_missing"
    with pytest.raises(VersionMissingError):
        resolve_version("   ")


def test_resolve_version_unsupported() -> None:
    with pytest.raises(VersionUnsupportedError) as ei:
        resolve_version("v3")
    assert ei.value.code.value == "version_unsupported"


def test_v1_shape_and_v2_defaults(service, caplog) -> None:
    """v1 返回结构不变；v2 未提供 priority 时稳定默认 normal。"""
    r1, replayed1 = service.create_order(
        version_raw="v1", idempotency_key="k-v1",
        payload={"item": "book", "quantity": 2}, request_id="req-v1",
    )
    assert replayed1 is False
    body1 = service.to_response(r1, ApiVersion.V1)
    # 旧版本结构只有三个字段，语义不被破坏
    assert body1 == {"order_id": r1.order_id, "item": "book", "quantity": 2}

    # v2 缺省 priority -> normal，且响应是 v1 超集
    r2, _ = service.create_order(
        version_raw="v2", idempotency_key="k-v2",
        payload={"item": "pen", "quantity": 1}, request_id="req-v2",
    )
    body2 = service.to_response(r2, ApiVersion.V2)
    assert body2["priority"] == "normal"
    assert body2["api_version"] == "v2"
    # v2 是 v1 字段的超集
    assert set(body2) > set(body1)
    # v2 显式 high 生效
    r3, _ = service.create_order(
        version_raw="v2", idempotency_key="k-v3",
        payload={"item": "ink", "quantity": 5, "priority": "high"},
        request_id="req-v3",
    )
    assert service.to_response(r3, ApiVersion.V2)["priority"] == "high"

    # 日志含请求标识 / 幂等键 / 判定结论与依据
    decisions = [getattr(rec, "decision", None) for rec in caplog.records]
    assert "VERSION_RESOLVED" in decisions
    assert any(getattr(rec, "idem_key", "-") == "k-v1" for rec in caplog.records)
    assert any("header accepted" in rec.getMessage() for rec in caplog.records)


def test_invalid_field_rejected(service) -> None:
    """数量非法 / 类型错误 / 未知字段必须以 INVALID_FIELD 拒绝，错误可区分。"""
    cases = [
        {"item": "", "quantity": 1},
        {"item": "x", "quantity": 0},
        {"item": "x", "quantity": "1"},   # 不允许字符串数字静默转换
        {"quantity": 1},                  # 缺字段
        {"item": "x", "quantity": 1, "priority": "high"},  # v1 不认识新字段
    ]
    for payload in cases:
        with pytest.raises(InvalidFieldError) as ei:
            service.create_order(
                version_raw="v1", idempotency_key="k-invalid",
                payload=payload, request_id="req-invalid",
            )
        assert ei.value.code.value == "invalid_field"
        assert ei.value.message  # 带具体字段原因

    with pytest.raises(InvalidFieldError) as ei:
        service.create_order(
            version_raw="v2", idempotency_key="k-invalid2",
            payload={"item": "x", "quantity": 1, "priority": "urgent"},
        )
    assert "priority" in ei.value.message


def test_version_errors_distinguishable(service) -> None:
    """缺失与不支持是两种可区分原因。"""
    with pytest.raises(VersionMissingError) as a:
        service.create_order(version_raw=None, idempotency_key="k",
                             payload={"item": "a", "quantity": 1})
    with pytest.raises(VersionUnsupportedError) as b:
        service.create_order(version_raw="v9", idempotency_key="k",
                             payload={"item": "a", "quantity": 1})
    assert a.value.code.value != b.value.code.value


def test_cross_version_key_is_distinct(service) -> None:
    """指纹包含版本：同一幂等键在 v1/v2 下视为不同请求，冲突被明确拒绝。"""
    service.create_order(version_raw="v1", idempotency_key="cross",
                         payload={"item": "a", "quantity": 1},
                         request_id="cv1")
    # v2 默认 priority=normal，但版本不同 -> 指纹不同 -> 冲突而非覆盖
    with pytest.raises(IdempotencyConflictError) as ei:
        service.create_order(version_raw="v2", idempotency_key="cross",
                             payload={"item": "a", "quantity": 1},
                             request_id="cv2")
    assert ei.value.code.value == "idempotency_conflict"
    assert service.order_count() == 1
