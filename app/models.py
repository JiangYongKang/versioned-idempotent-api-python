"""请求 / 响应数据模型。

版本兼容规则：
- v1 只接受 ``item`` 与 ``quantity``，多传/错传字段一律拒绝；
  返回结构固定为 ``order_id/item/quantity``，字段语义永不改变。
- v2 在 v1 基础上新增可选 ``priority``（``normal``/``high``），
  未提供时稳定取默认 ``"normal"``；返回为 v1 的超集，
  追加 ``priority`` 与 ``api_version``。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.errors import InvalidFieldError
from app.versioning import ApiVersion


class Priority(str):
    NORMAL = "normal"
    HIGH = "high"


ALLOWED_PRIORITIES = frozenset({Priority.NORMAL, Priority.HIGH})


class CreateOrderRequest:
    """统一内部请求模型。"""

    __slots__ = ("item", "quantity", "priority")

    def __init__(self, item: str, quantity: int, priority: str = Priority.NORMAL):
        self.item = item
        self.quantity = quantity
        self.priority = priority

    def fingerprint_parts(self) -> tuple[str, int, str]:
        return (self.item, self.quantity, self.priority)


class _StrictV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    item: str = Field(min_length=1)
    quantity: int = Field(ge=1)


class _V2(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    item: str = Field(min_length=1)
    quantity: int = Field(ge=1)
    priority: str = Priority.NORMAL


def _normalize_validation_error(exc: ValidationError) -> InvalidFieldError:
    first = exc.errors()[0]
    loc = ".".join(str(p) for p in first.get("loc", ())) or "payload"
    return InvalidFieldError(f"invalid field {loc!r}: {first.get('msg', 'invalid')}")


def parse_payload(version: ApiVersion, payload: dict[str, Any]) -> CreateOrderRequest:
    """按版本校验并转换载荷，任何非法输入归一化为 InvalidFieldError。"""
    if version is ApiVersion.V1:
        try:
            m = _StrictV1.model_validate(payload)
        except ValidationError as e:
            raise _normalize_validation_error(e) from None
        return CreateOrderRequest(item=m.item, quantity=m.quantity,
                                  priority=Priority.NORMAL)

    # v2
    try:
        m = _V2.model_validate(payload)
    except ValidationError as e:
        raise _normalize_validation_error(e) from None
    if m.priority not in ALLOWED_PRIORITIES:  # 理论上枚举已挡，双保险
        raise InvalidFieldError(
            f"invalid field 'priority': must be one of {sorted(ALLOWED_PRIORITIES)}"
        )
    return CreateOrderRequest(item=m.item, quantity=m.quantity, priority=m.priority)


class OrderResult:
    """内部统一结果。"""

    __slots__ = ("order_id", "item", "quantity", "priority", "api_version")

    def __init__(self, order_id: str, item: str, quantity: int,
                 priority: str, api_version: str):
        self.order_id = order_id
        self.item = item
        self.quantity = quantity
        self.priority = priority
        self.api_version = api_version


def build_response(result: OrderResult, api_version: str) -> dict:
    """按版本构造对外返回结构。

    v1 永远只有三个原始字段（旧结构不被破坏）；
    v2 是其超集，追加 priority / api_version。
    """
    base = {
        "order_id": result.order_id,
        "item": result.item,
        "quantity": result.quantity,
    }
    if api_version == ApiVersion.V2.value:
        base["priority"] = result.priority
        base["api_version"] = result.api_version
        return base
    return base
