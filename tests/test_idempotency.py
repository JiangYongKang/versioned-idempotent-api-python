"""幂等语义：重复提交重放、同键并发一致、副作用一次、冲突可区分。"""

from __future__ import annotations

import asyncio

from versioned_orders.service import GovernanceConfig, OrderService, RequestContext

V1_BODY = {"customer_id": "c1", "sku": "sku-1", "quantity": 2, "amount_cents": 1500}


def _make_service() -> OrderService:
    # 预留足够并发额度，本文件关注的是“同键”而不是全局上限
    return OrderService(GovernanceConfig(max_inflight=16, request_timeout=5.0))


def test_duplicate_submission_replays_single_side_effect() -> None:
    async def scenario():
        svc = _make_service()
        body = dict(V1_BODY)
        r1 = await svc.create_order(body, RequestContext("req-1", "dup-key"),
                                    path_version="v1")
        r2 = await svc.create_order(body, RequestContext("req-2", "dup-key"),
                                    path_version="v1")
        assert r1["ok"] and r2["ok"]
        assert r1["replayed"] is False and r2["replayed"] is True
        # 结论一致：同一条订单
        assert r1["data"] == r2["data"]
        # 副作用只生效一次
        assert svc.order_count() == 1
        assert svc.payment.charge_count == 1
        assert svc.inventory.reservation_count == 1
    asyncio.run(scenario())


def test_concurrent_same_key_single_write_consistent_results() -> None:
    async def scenario():
        svc = _make_service()
        # 让首个请求在依赖处停留，制造真实在途并发
        svc.payment = type(svc.payment)()
        injected = {"payment": {"delay": 0.15}}
        ctx_factory = lambda i: RequestContext(
            f"req-c{i}", "concurrent-key", injected=injected)
        results = await asyncio.gather(*[
            svc.create_order(dict(V1_BODY), ctx_factory(i), path_version="v2")
            for i in range(8)
        ])
        assert all(r["ok"] for r in results), results
        order_ids = {r["data"]["order_id"] for r in results}
        assert len(order_ids) == 1                      # 结论一致
        assert results[0]["data"] == results[-1]["data"]
        flags = sorted(r["replayed"] for r in results)
        assert flags == sorted([False] + [True] * 7)   # 恰好一次首次执行
        assert svc.order_count() == 1                  # 无重复写入
        assert svc.payment.charge_count == 1
        assert svc.inventory.reservation_count == 1
    asyncio.run(scenario())


def test_same_key_different_body_is_conflict() -> None:
    async def scenario():
        svc = _make_service()
        body_a = dict(V1_BODY)
        body_b = dict(V1_BODY, amount_cents=9999)
        r1 = await svc.create_order(body_a, RequestContext("req-a", "k"),
                                    path_version="v1")
        r2 = await svc.create_order(body_b, RequestContext("req-b", "k"),
                                    path_version="v1")
        assert r1["ok"]
        assert not r2["ok"]
        assert r2["error"]["code"] == "IDEMPOTENCY_REPLAY_CONFLICT"
        assert r2["http_status"] == 409
    asyncio.run(scenario())


def test_failed_outcome_is_replayed_with_same_error() -> None:
    async def scenario():
        svc = _make_service()
        inj = {"payment": {"fail": True, "fail_category": "card_declined"}}
        r1 = await svc.create_order(
            dict(V1_BODY),
            RequestContext("req-f1", "fail-key", injected=inj),
            path_version="v1")
        r2 = await svc.create_order(
            dict(V1_BODY), RequestContext("req-f2", "fail-key"),
            path_version="v1")
        assert not r1["ok"] and not r2["ok"]
        assert r1["error"]["code"] == r2["error"]["code"] == "UPSTREAM_ERROR"
        assert r2["replayed"] is True
        assert svc.order_count() == 0                 # 失败不留中间状态
        assert svc.payment.charge_count == 0
    asyncio.run(scenario())


def test_decision_logs_carry_request_id_key_and_reason(caplog) -> None:
    import logging
    caplog.set_level(logging.INFO, logger="versioned_orders")
    async def scenario():
        svc = _make_service()
        await svc.create_order(dict(V1_BODY),
                               RequestContext("req-log-1", "log-key"),
                               path_version="v2")
    asyncio.run(scenario())
    # 直接从 record 上取注入的 gov 字段（formatter 是给 stderr 的）
    govs = [getattr(rec, "gov", {}) for rec in caplog.records
            if getattr(rec, "gov", None)]
    assert any(g.get("request_id") == "req-log-1"
               and g.get("idempotency_key") == "log-key"
               for g in govs)
    decisions = {g.get("decision") for g in govs}
    assert {"version_resolved", "idem_first", "committed"} <= decisions
    # 每条判定都有可解释依据
    assert all(g.get("reason") for g in govs)
