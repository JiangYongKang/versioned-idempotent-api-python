"""多种入口（服务直调、HTTP、自定义函数式入口）下的一致结论。"""

from __future__ import annotations

import asyncio

from httpx import ASGITransport, AsyncClient

from versioned_orders.api import create_app
from versioned_orders.service import GovernanceConfig, OrderService, RequestContext

BODY = {"customer_id": "c1", "sku": "sku-1", "quantity": 1, "amount_cents": 500}


async def _functional_entry(service: OrderService, key: str) -> dict:
    """模拟另一个非 HTTP 入口（比如消息队列消费者）：同样走唯一核心方法。"""
    return await service.create_order(
        BODY, RequestContext(f"fn-{key}", key, entrypoint="consumer"),
        header_version="v2",
    )


def test_same_key_across_entries_replays_identical_outcome() -> None:
    async def scenario():
        service = OrderService(GovernanceConfig(max_inflight=8, request_timeout=5))
        app = create_app(service)  # 三个入口共享同一服务实例与状态

        # 入口 1：HTTP 先到
        async with AsyncClient(transport=ASGITransport(app=app),
                               base_url="http://t") as c:
            r_http = await c.post(
                "/v2/orders", json=BODY,
                headers={"Idempotency-Key": "shared-key"})
        assert r_http.status_code == 200

        # 入口 2：服务直调，同键重放
        r_direct = await service.create_order(
            BODY, RequestContext("direct-1", "shared-key"),
            path_version="v2")
        # 入口 3：函数式入口，同键重放
        r_fn = await _functional_entry(service, "shared-key")

        assert r_direct["ok"] and r_fn["ok"]
        assert r_direct["replayed"] is True and r_fn["replayed"] is True
        http_data = r_http.json()["data"]
        assert r_direct["data"]["order_id"] == http_data["order_id"]
        assert r_fn["data"]["order_id"] == http_data["order_id"]
        assert service.order_count() == 1
        assert service.payment.charge_count == 1
    asyncio.run(scenario())


def test_app_factory_builds_without_running_server() -> None:
    from versioned_orders.api import create_app as factory
    app = factory()
    paths = {route.path for route in app.routes}
    assert "/v1/orders" in paths and "/v2/orders" in paths
