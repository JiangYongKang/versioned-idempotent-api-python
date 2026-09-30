"""请求 / 响应数据模型（按版本定义字段语义）。

兼容规则：
- v1 的字段集合、必填性与返回结构是冻结契约，永不改变。
- v2 继承 v1 全部字段，只做增量：新增字段在缺省时给出确定稳定的默认值。
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Priority(str, Enum):
    NORMAL = "normal"
    HIGH = "high"


class CreateOrderV1(BaseModel):
    """v1 创建订单：旧版本字段，语义保持不变。"""

    customer_id: str = Field(min_length=1)
    sku: str = Field(min_length=1)
    quantity: int = Field(ge=1, le=10_000)
    amount_cents: int = Field(ge=1)
    note: str = ""


class CreateOrderV2(CreateOrderV1):
    """v2 创建订单：继承 v1 全部字段；新增字段缺省稳定。"""

    # 新增：优先级，缺省固定为 normal（不会随机、不会随环境变化）
    priority: Priority = Priority.NORMAL
    # 新增：优惠码，缺省固定为 None
    coupon_code: str | None = Field(default=None, max_length=64)


# 各版本字段集合（返回结构也按此裁剪，保证 v1 结构不被新字段污染）
V1_FIELDS: frozenset[str] = frozenset(CreateOrderV1.model_fields)
V2_FIELDS: frozenset[str] = frozenset(CreateOrderV2.model_fields)


def validate_and_normalize(version: str, raw: Any) -> dict[str, Any]:
    """按版本校验请求体，返回规范化后的纯 dict（已填默认值）。

    校验失败时由上层捕获 pydantic.ValidationError 并转成 FIELD_INVALID，
    本模块不依赖 errors 层。
    """
    model_cls = CreateOrderV1 if version == "v1" else CreateOrderV2
    model = model_cls.model_validate(raw)
    data = model.model_dump()
    if version == "v2":
        data["priority"] = Priority(data["priority"]).value
    return data


class OrderView(BaseModel):
    """统一的订单资源视图（超集）；对外输出时按版本裁剪字段。"""

    order_id: str
    version: str
    status: str
    customer_id: str
    sku: str
    quantity: int
    amount_cents: int
    note: str = ""
    # v2 新增字段（在 v1 视图中不会出现）
    priority: str = Priority.NORMAL.value
    coupon_code: str | None = None
    charge_id: str
    reservation_id: str

    @classmethod
    def from_record(cls, record: dict[str, Any], version: str) -> "OrderView":
        return cls(**record)

    def to_version_dict(self, version: str) -> dict[str, Any]:
        """按版本裁剪返回结构：v1 永远只有 v1 的字段。"""
        keep = V1_FIELDS if version == "v1" else V2_FIELDS
        base = {
            "order_id": self.order_id,
            "version": self.version,
            "status": self.status,
            "charge_id": self.charge_id,
            "reservation_id": self.reservation_id,
        }
        for name in keep:
            base[name] = getattr(self, name)
        return base
