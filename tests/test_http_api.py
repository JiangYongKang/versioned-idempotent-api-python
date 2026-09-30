"""HTTP 入口：路径版本 / 头版本 / 无版本通用入口 结论一致；错误码透出正确。

为避免引入 pytest-asyncio 等额外依赖，异步测试统一在 asyncio.run 内执行。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient

from versioned_orders.api import create_app
from versioned_orders.service import GovernanceConfig, OrderService

V1_BODY = {"customer_id": "c1", "sku": "sku-1", "quantity": 2, "amount_cents": 1500}


def _app():
    return create_app(OrderService(
        GovernanceConfig(max_inflight=4, request_timeout=5)))


def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


def test_three_version_entries_equivalent() -> None:
    async def scenario():
        app = _app()
        async with _client(app) as c:
            r_path = await c.post("/v2/orders", json=V1_BODY,
                                  headers={"Idempotency-Key": "http-1"})
            r_header = await c.post("/orders", json=V1_BODY, headers={
                "Idempotency-Key": "http-2", "X-API-Version": "v2"})
            # 通用入口 + 头版本重放路径入口的订单（换键则是新建；这里换键验证同构）
            assert r_path.status_code == r_header.status_code == 200
            d1, d2 = r_path.json()["data"], r_header.json()["data"]
            assert d1["priority"] == d2["priority"] == "normal"
            assert set(d1) == set(d2)

            # 通用入口缺头 -> VERSION_MISSING
            r = await c.post("/orders", json=V1_BODY,
                             headers={"Idempotency-Key": "http-3"})
            assert r.status_code == 400
            assert r.json()["error"]["code"] == "VERSION_MISSING"
    asyncio.run(scenario())


def test_http_error_matrix_is_distinguishable() -> None:
    async def scenario():
        app = _app()
        async with _client(app) as c:
            cases = [
                ("POST", "/v3/orders", {"Idempotency-Key": "k"}, V1_BODY,
                 400, "VERSION_UNSUPPORTED"),
                ("POST", "/v1/orders", {}, V1_BODY,
                 400, "IDEMPOTENCY_KEY_MISSING"),
                ("POST", "/v1/orders", {"Idempotency-Key": "k2"}, {"bad": 1},
                 422, "FIELD_INVALID"),
                ("POST", "/v1/orders",
                 {"Idempotency-Key": "k3", "X-API-Version": "v2"}, V1_BODY,
                 400, "VERSION_CONFLICT"),
            ]
            for method, url, headers, json_body, want_status, want_code in cases:
                r = await c.request(method, url, headers=headers, json=json_body)
                assert r.status_code == want_status, (url, r.status_code, r.text)
                assert r.json()["error"]["code"] == want_code, url
    asyncio.run(scenario())


def test_http_replay_over_network_and_timeout_header() -> None:
    async def scenario():
        app = _app()
        async with _client(app) as c:
            headers = {"Idempotency-Key": "net-1",
                       "X-Request-Timeout": "0.05",
                       "X-Debug-Inject": json.dumps(
                           {"payment": {"delay": 0.4}})}
            r1 = await c.post("/v1/orders", headers=headers, json=V1_BODY)
            assert r1.status_code == 504
            assert r1.json()["error"]["code"] == "TIMEOUT"
            # 网络重试（不带注入与超时头）仍重放同一结论
            r2 = await c.post("/v1/orders",
                              headers={"Idempotency-Key": "net-1"},
                              json=V1_BODY)
            assert r2.status_code == 504
            body = r2.json()
            assert body["error"]["code"] == "TIMEOUT"
            assert body["replayed"] is True
            assert body["request_id"] != r1.json()["request_id"]
    asyncio.run(scenario())


def test_http_concurrent_same_key_single_order() -> None:
    async def scenario():
        app = create_app(OrderService(
            GovernanceConfig(max_inflight=16, request_timeout=5)))
        async with _client(app) as c:
            headers = {
                "Idempotency-Key": "net-concurrent",
                "X-Debug-Inject": json.dumps({"payment": {"delay": 0.2}}),
            }
            responses = await asyncio.gather(*[
                c.post("/v2/orders", headers=headers, json=V1_BODY)
                for _ in range(6)
            ])
            assert all(r.status_code == 200 for r in responses)
            ids = {r.json()["data"]["order_id"] for r in responses}
            assert len(ids) == 1
            replayed = sum(r.json()["replayed"] for r in responses)
            assert replayed == 5
    asyncio.run(scenario())


def test_read_back_uses_versioned_view() -> None:
    async def scenario():
        app = _app()
        async with _client(app) as c:
            r = await c.post("/v2/orders", json=V1_BODY,
                             headers={"Idempotency-Key": "view-1"})
            order_id = r.json()["data"]["order_id"]
            v1 = await c.get(f"/v1/orders/{order_id}")
            v2 = await c.get(f"/v2/orders/{order_id}")
            assert "priority" not in v1.json()["data"]
            assert v2.json()["data"]["priority"] == "normal"

            missing = await c.get("/v1/orders/nope")
            assert missing.status_code == 404
            assert missing.json()["error"]["code"] == "NOT_FOUND"
    asyncio.run(scenario())


def test_http_idempotency_conflict_and_missing_key_distinct() -> None:
    async def scenario():
        app = _app()
        async with _client(app) as c:
            await c.post("/v1/orders", json=V1_BODY,
                         headers={"Idempotency-Key": "dup-http"})
            # 同键不同体
            changed = dict(V1_BODY, amount_cents=V1_BODY["amount_cents"] + 1)
            r = await c.post("/v1/orders", json=changed,
                             headers={"Idempotency-Key": "dup-http"})
            assert r.status_code == 409
            assert r.json()["error"]["code"] == "IDEMPOTENCY_REPLAY_CONFLICT"
            assert r.json()["idempotency_key"] == "dup-http"
    asyncio.run(scenario())
