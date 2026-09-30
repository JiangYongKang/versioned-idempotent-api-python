"""本地状态模拟的外部依赖：支付网关与库存服务。

用内存状态模拟，支持通过故障注入（延迟 / 失败）测试超时与异常归一化。
- charge/reserve 按确定性幂等号去重，同一号只生效一次；
- 失败与延迟以 ``UpstreamError`` / ``asyncio.sleep`` 模拟，service 层负责
  把它们归一化为 UPSTREAM_ERROR / TIMEOUT / CANCELLED，不透出内部异常；
- 资源写入只发生在两个依赖都成功之后，依赖失败绝不留中间资源状态。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass


class UpstreamError(RuntimeError):
    """模拟的外部依赖业务失败（受控，service 层会归一化）。"""

    def __init__(self, dependency: str, category: str) -> None:
        super().__init__(f"{dependency}:{category}")
        self.dependency = dependency
        self.category = category


@dataclass
class UpstreamConfig:
    """单次调用的故障注入配置。"""

    delay: float = 0.0          # 模拟依赖处理耗时
    fail: bool = False          # 模拟依赖返回失败
    fail_category: str = "simulated_failure"

    @classmethod
    def from_injection(cls, injected: dict, stage: str) -> "UpstreamConfig | None":
        """从请求级故障注入表中读取某阶段配置；没有则返回 None。"""
        if not injected or stage not in injected:
            return None
        raw = injected[stage] or {}
        return cls(
            delay=float(raw.get("delay", 0.0)),
            fail=bool(raw.get("fail", False)),
            fail_category=str(raw.get("fail_category", "simulated_failure")),
        )


class MockPaymentGateway:
    """模拟支付网关：charge 对同一 charge_id 只生效一次。"""

    NAME = "payment_gateway"

    def __init__(self) -> None:
        self._charges: set[str] = set()
        self._lock = asyncio.Lock()

    async def charge(self, charge_id: str, amount_cents: int,
                     config: UpstreamConfig | None = None) -> dict:
        if config is not None and config.delay:
            await asyncio.sleep(config.delay)  # 取消会在这里抛出 CancelledError
        if config is not None and config.fail:
            raise UpstreamError(self.NAME, config.fail_category)
        async with self._lock:
            first_time = charge_id not in self._charges
            self._charges.add(charge_id)
        return {
            "charge_id": charge_id,
            "amount_cents": amount_cents,
            "status": "captured",
            "first_time": first_time,
        }

    @property
    def charge_count(self) -> int:
        return len(self._charges)


class MockInventoryService:
    """模拟库存服务：reserve 对同一 reservation_id 只生效一次。"""

    NAME = "inventory_service"

    def __init__(self) -> None:
        self._reservations: set[str] = set()
        self._lock = asyncio.Lock()

    async def reserve(self, reservation_id: str, sku: str,
                      config: UpstreamConfig | None = None) -> dict:
        if config is not None and config.delay:
            await asyncio.sleep(config.delay)
        if config is not None and config.fail:
            raise UpstreamError(self.NAME, config.fail_category)
        async with self._lock:
            first_time = reservation_id not in self._reservations
            self._reservations.add(reservation_id)
        return {
            "reservation_id": reservation_id,
            "sku": sku,
            "status": "reserved",
            "first_time": first_time,
        }

    @property
    def reservation_count(self) -> int:
        return len(self._reservations)
