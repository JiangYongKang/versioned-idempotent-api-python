"""版本治理与字段语义：缺失 / 不支持 / 冲突 / 非法字段 / 新旧版本兼容。"""

from __future__ import annotations

import asyncio

import pytest

from versioned_orders.errors import AppError
from versioned_orders.service import GovernanceConfig, OrderService, RequestContext
from versioned_orders.versioning import resolve_version, validate_payload

V1_BODY = {"customer_id": "c1", "sku": "sku-1", "quantity": 2, "amount_cents": 1500}


def test_resolve_version_matrix() -> None:
    assert resolve_version("v1", None) == "v1"
    assert resolve_version(None, "v2") == "v2"
    assert resolve_version("v2", "v2") == "v2"

    def expect(path, header, code):
        with pytest.raises(AppError) as ei:
            resolve_version(path, header)
        assert ei.value.code == code, ei.value.code

    expect(None, None, "VERSION_MISSING")
    expect("v3", None, "VERSION_UNSUPPORTED")
    expect(None, "v9", "VERSION_UNSUPPORTED")
    expect("v1", "v2", "VERSION_CONFLICT")


def test_v1_fields_frozen_and_v2_defaults_stable() -> None:
    d1 = validate_payload("v1", V1_BODY)
    assert set(d1) == {"customer_id", "sku", "quantity", "amount_cents", "note"}
    d2_a = validate_payload("v2", V1_BODY)
    d2_b = validate_payload("v2", V1_BODY)
    # 新增字段在未提供时默认确定且稳定
    assert d2_a["priority"] == "normal"
    assert d2_a["coupon_code"] is None
    assert d2_a == d2_b
    # v1 永远不会出现 v2 的新字段
    assert "priority" not in d1 and "coupon_code" not in d1


def test_field_invalid_is_distinguishable() -> None:
    cases = [
        ({"customer_id": "", "sku": "s", "quantity": 1, "amount_cents": 1},
         "customer_id"),
        ({"customer_id": "c", "sku": "s", "quantity": 0, "amount_cents": 1},
         "quantity"),
        ({"customer_id": "c", "sku": "s", "quantity": 1, "amount_cents": -5},
         "amount_cents"),
        ({"customer_id": "c", "sku": "s", "quantity": 1, "amount_cents": 1,
          "priority": "urgent"}, "priority"),
    ]
    for body, field in cases:
        with pytest.raises(AppError) as ei:
            validate_payload("v2", body)
        assert ei.value.code == "FIELD_INVALID"
        assert ei.value.details["field"] == field


def test_service_level_version_errors_are_structured() -> None:
    async def scenario():
        svc = OrderService(GovernanceConfig(max_inflight=4))
        # 无版本
        out = await svc.create_order(V1_BODY, RequestContext("r", "k"),
                                     path_version=None, header_version=None)
        assert out["ok"] is False and out["error"]["code"] == "VERSION_MISSING"
        assert out["http_status"] == 400
        # 不支持版本
        out = await svc.create_order(V1_BODY, RequestContext("r", "k"),
                                     path_version="v3")
        assert out["error"]["code"] == "VERSION_UNSUPPORTED"
        assert "supported" in out["error"]["details"]
        # 版本冲突
        out = await svc.create_order(V1_BODY, RequestContext("r", "k"),
                                     path_version="v1", header_version="v2")
        assert out["error"]["code"] == "VERSION_CONFLICT"

    asyncio.run(scenario())
