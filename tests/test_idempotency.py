"""幂等去重：重复提交、同键并发一致、指纹冲突、缺键拒绝。"""

from __future__ import annotations

import threading

import pytest

from app.service import OrderService

from app.errors import (
    IdempotencyConflictError,
    IdempotencyKeyMissingError,
)


def test_duplicate_idempotency_key_single_effect(service, caplog) -> None:
    """同一幂等键串行重复提交：副作用一次，结果一致。"""
    kwargs = dict(version_raw="v1",
                  payload={"item": "book", "quantity": 2})
    r1, rep1 = service.create_order(idempotency_key="dup-1", request_id="r1",
                                    **kwargs)
    r2, rep2 = service.create_order(idempotency_key="dup-1", request_id="r2",
                                    **kwargs)
    assert rep1 is False and rep2 is True
    assert r1.order_id == r2.order_id
    # 副作用只生效一次
    assert service.order_count() == 1
    assert service.warehouse.count() == 1

    decisions = [getattr(r, "decision", None) for r in caplog.records]
    assert "COMMIT" in decisions and "REPLAY" in decisions
    # 日志中可按请求标识 / 幂等键 / 依据解释结论
    replay = next(r for r in caplog.records
                  if getattr(r, "decision", None) == "REPLAY")
    assert getattr(replay, "request_id") == "r2"
    assert getattr(replay, "idem_key") == "dup-1"
    assert "already completed" in replay.getMessage()


def test_concurrent_same_key_single_write() -> None:
    """同一幂等键并发：只产生一次外部写入，所有响应一致。"""
    service = OrderService(max_in_flight=8, request_timeout=2.0)
    service.warehouse.delay = 0.3
    n = 6
    barrier = threading.Barrier(n)
    results: list[tuple[str, bool]] = []
    errors: list[Exception] = []

    def worker(i: int) -> None:
        barrier.wait()
        try:
            r, rep = service.create_order(
                version_raw="v1", idempotency_key="CONC",
                payload={"item": "widget", "quantity": 3},
                request_id=f"c{i}",
            )
            results.append((r.order_id, rep))
        except Exception as exc:  # noqa: BLE001 - 测试收集
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    order_ids = {oid for oid, _ in results}
    assert len(order_ids) == 1
    assert sum(1 for _, rep in results if rep) == n - 1
    assert service.order_count() == 1
    assert service.warehouse.count() == 1


def test_same_key_different_payload_conflict(service) -> None:
    """同键不同请求体 -> 幂等冲突，不产生额外副作用。"""
    service.create_order(version_raw="v1", idempotency_key="conf",
                         payload={"item": "book", "quantity": 1},
                         request_id="p1")
    with pytest.raises(IdempotencyConflictError) as ei:
        service.create_order(version_raw="v1", idempotency_key="conf",
                             payload={"item": "book", "quantity": 99},
                             request_id="p2")
    assert ei.value.code.value == "idempotency_conflict"
    assert service.order_count() == 1  # 没有第二次写入


def test_concurrent_conflict_with_inflight_key(service) -> None:
    """键还在途时到达一个指纹不同的请求：立即冲突，且不影响在途提交。"""
    service.warehouse.delay = 0.3
    outcome: dict = {}

    def leader() -> None:
        try:
            r, _ = service.create_order(
                version_raw="v1", idempotency_key="K",
                payload={"item": "a", "quantity": 1}, request_id="leader",
            )
            outcome["order"] = r.order_id
        except Exception as exc:  # noqa: BLE001
            outcome["error"] = exc

    t = threading.Thread(target=leader)
    t.start()
    import time
    time.sleep(0.1)
    with pytest.raises(IdempotencyConflictError):
        service.create_order(
            version_raw="v1", idempotency_key="K",
            payload={"item": "DIFFERENT", "quantity": 2}, request_id="rival",
        )
    t.join()
    assert "order" in outcome and "error" not in outcome
    assert service.order_count() == 1


def test_missing_idempotency_key(service) -> None:
    with pytest.raises(IdempotencyKeyMissingError) as ei:
        service.create_order(
            version_raw="v1", idempotency_key="   ",
            payload={"item": "a", "quantity": 1},
        )
    assert ei.value.code.value == "idempotency_key_missing"
    assert service.order_count() == 0
