"""并发治理：全局在途超限立即拒绝；关停期间拒绝新请求且在途请求有确定结论。"""

from __future__ import annotations

import asyncio

from versioned_orders.service import GovernanceConfig, OrderService, RequestContext

V1_BODY = {"customer_id": "c1", "sku": "sku-1", "quantity": 1, "amount_cents": 100}


async def _hold(service: OrderService, key: str, delay: float,
                request_id: str) -> dict:
    return await service.create_order(
        dict(V1_BODY),
        RequestContext(request_id, key, injected={"payment": {"delay": delay}}),
        path_version="v1",
    )


def test_inflight_over_limit_is_rejected_not_queued() -> None:
    async def scenario():
        svc = OrderService(GovernanceConfig(max_inflight=2, request_timeout=5))
        # 先占满 2 个在途槽（不同幂等键，真正执行）
        holders = asyncio.gather(
            _hold(svc, "occupy-1", 0.3, "occ-1"),
            _hold(svc, "occupy-2", 0.3, "occ-2"),
        )
        await asyncio.sleep(0.05)
        assert svc.limiter.inflight == 2

        # 超限请求：立即、明确地拒绝，不排队
        rejected = await _hold(svc, "occupy-3", 0.0, "occ-3")
        assert rejected["ok"] is False
        assert rejected["error"]["code"] == "CONCURRENCY_LIMIT"
        assert rejected["http_status"] == 429
        assert rejected["error"]["details"]["max_inflight"] == 2

        results = await holders
        assert all(r["ok"] for r in results)
        # 槽位释放，恢复可用
        await asyncio.sleep(0.01)
        assert svc.limiter.inflight == 0
    asyncio.run(scenario())


def test_shutdown_rejects_new_and_drains_inflight() -> None:
    async def scenario():
        svc = OrderService(GovernanceConfig(
            max_inflight=8, request_timeout=5, shutdown_grace=5))
        holder = asyncio.ensure_future(
            _hold(svc, "drain-1", 0.2, "drain-req"))
        await asyncio.sleep(0.05)

        # 发起关停
        shutdown_task = asyncio.ensure_future(svc.shutdown())
        await asyncio.sleep(0.02)

        # 关停后新请求被明确拒绝
        late = await svc.create_order(
            dict(V1_BODY), RequestContext("late", "late-key"),
            path_version="v1")
        assert not late["ok"]
        assert late["error"]["code"] == "SHUTDOWN"
        assert late["http_status"] == 503

        drained = await shutdown_task
        assert drained is True                  # 在途请求处理完毕
        result = await holder
        assert result["ok"] is True             # 在途结论确定：成功
        assert svc.order_count() == 1
    asyncio.run(scenario())


def test_shutdown_grace_expiry_is_deterministic_failure() -> None:
    async def scenario():
        svc = OrderService(GovernanceConfig(
            max_inflight=8, request_timeout=5, shutdown_grace=0.05))
        # gate 直接跟踪一个长时间任务（模拟请求卡住），宽限期到后必须给出结论
        await svc.gate.enter()

        async def long_work():
            try:
                await asyncio.sleep(1.0)
            finally:
                await svc.gate.leave()

        task = asyncio.ensure_future(long_work())
        await svc.gate.begin_shutdown()
        drained = await svc.gate.wait_drained()
        assert drained is False                 # 没有无限等待：明确告知未排空
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    asyncio.run(scenario())
