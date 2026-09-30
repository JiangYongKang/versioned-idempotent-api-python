"""超时 / 中途取消 / 依赖失败的归一化：错误类型可区分、不留中间状态。"""

from __future__ import annotations

import asyncio

from versioned_orders.service import GovernanceConfig, OrderService, RequestContext

V1_BODY = {"customer_id": "c1", "sku": "sku-1", "quantity": 1, "amount_cents": 100}


def test_timeout_returns_timeout_error_and_no_partial_state() -> None:
    async def scenario():
        svc = OrderService(GovernanceConfig(max_inflight=4, request_timeout=5))
        out = await svc.create_order(
            dict(V1_BODY),
            RequestContext("to-1", "timeout-key",
                           timeout=0.05,
                           injected={"payment": {"delay": 0.4}}),
            path_version="v1",
        )
        assert out["ok"] is False
        assert out["error"]["code"] == "TIMEOUT"
        assert out["http_status"] == 504
        assert out["error"]["details"]["timeout_seconds"] == 0.05
        assert svc.order_count() == 0          # 未提交订单
        assert svc.payment.charge_count == 0   # 支付在 sleep 阶段即超时
        # 同键再试得到相同的超时结论（重放），不会突然变成成功
        again = await svc.create_order(
            dict(V1_BODY), RequestContext("to-2", "timeout-key"),
            path_version="v1")
        assert again["error"]["code"] == "TIMEOUT"
        assert again["replayed"] is True
    asyncio.run(scenario())


def test_cancel_midflight_is_recorded_and_replayed() -> None:
    async def scenario():
        svc = OrderService(GovernanceConfig(max_inflight=4, request_timeout=5))
        task = asyncio.ensure_future(svc.create_order(
            dict(V1_BODY),
            RequestContext("cx-1", "cancel-key",
                           injected={"payment": {"delay": 0.5}}),
            path_version="v1",
        ))
        await asyncio.sleep(0.05)
        task.cancel()
        # 取消被归一化为可区分错误，内部 CancelledError 不透出
        out = await task
        assert out["ok"] is False
        assert out["error"]["code"] == "CANCELLED"
        assert out["http_status"] == 499
        assert svc.order_count() == 0           # 无中间状态

        # 结论已被确定性落定：并发等待者 / 后续重放拿到 CANCELLED，且只一份
        replay = await svc.create_order(
            dict(V1_BODY), RequestContext("cx-2", "cancel-key"),
            path_version="v1")
        assert replay["ok"] is False
        assert replay["error"]["code"] == "CANCELLED"
        assert replay["http_status"] == 499
        assert replay["replayed"] is True
        assert svc.order_count() == 0
    asyncio.run(scenario())


def test_waiter_cancellation_is_normalized_and_does_not_affect_outcome() -> None:
    async def scenario():
        svc = OrderService(GovernanceConfig(max_inflight=4, request_timeout=5))
        injected = {"payment": {"delay": 0.5}}
        # 首请求执行中
        first = asyncio.ensure_future(svc.create_order(
            dict(V1_BODY),
            RequestContext("cx-w0", "waiter-key", injected=injected),
            path_version="v1"))
        await asyncio.sleep(0.05)
        # 同键等待者到达，随后被它自己的调用方取消
        waiter = asyncio.ensure_future(svc.create_order(
            dict(V1_BODY),
            RequestContext("cx-w1", "waiter-key", injected=injected),
            path_version="v1"))
        await asyncio.sleep(0.05)
        waiter.cancel()
        wout = await waiter
        assert wout["error"]["code"] == "CANCELLED"
        assert wout["error"]["details"]["stage"] == "waiting_for_original"

        # 首请求不受影响，正常提交；副作用仅一次
        fout = await first
        assert fout["ok"] is True
        assert svc.order_count() == 1
        assert svc.payment.charge_count == 1
    asyncio.run(scenario())


def test_upstream_failure_is_normalized_without_leaking_internals() -> None:
    async def scenario():
        svc = OrderService(GovernanceConfig(max_inflight=4, request_timeout=5))
        out = await svc.create_order(
            dict(V1_BODY),
            RequestContext("up-1", "up-key",
                           injected={"payment": {"fail": True,
                                                 "fail_category": "card_lost"}}),
            path_version="v1",
        )
        assert out["error"]["code"] == "UPSTREAM_ERROR"
        assert out["http_status"] == 502
        assert out["error"]["details"]["dependency"] == "payment_gateway"
        # 对外不含内部异常类名/堆栈
        flat = str(out["error"])
        assert "UpstreamError" not in flat and "Traceback" not in flat
        assert svc.order_count() == 0
        # inventory 阶段失败也归一化
        out2 = await svc.create_order(
            dict(V1_BODY),
            RequestContext("up-2", "up-key-2",
                           injected={"inventory": {"fail": True}}),
            path_version="v1",
        )
        assert out2["error"]["code"] == "UPSTREAM_ERROR"
        assert out2["error"]["details"]["dependency"] == "inventory_service"
        assert svc.payment.charge_count == 1    # 仅第一次成功扣款那笔
        assert svc.order_count() == 0           # 但库存失败不写订单
    asyncio.run(scenario())


def test_error_codes_are_pairwise_distinguishable() -> None:
    async def scenario():
        svc = OrderService(GovernanceConfig(max_inflight=4))
        results = {}
        results["version"] = await svc.create_order(
            dict(V1_BODY), RequestContext("e1", "e1"))  # 无版本
        results["idem"] = await svc.create_order(
            dict(V1_BODY), RequestContext("e2"))         # 无版本且无键 -> 版本先拒
        # 幂等键缺失（版本合法）
        no_key = RequestContext("e3")
        results["no_key"] = await svc.create_order(
            dict(V1_BODY), no_key, path_version="v1")
        results["field"] = await svc.create_order(
            {"customer_id": "c"}, RequestContext("e4", "e4"),
            path_version="v1")
        codes = {name: out["error"]["code"]
                 for name, out in results.items()}
        assert codes["version"] == "VERSION_MISSING"
        assert codes["no_key"] == "IDEMPOTENCY_KEY_MISSING"
        assert codes["field"] == "FIELD_INVALID"
        assert len({codes["version"], codes["no_key"], codes["field"]}) == 3
    asyncio.run(scenario())
