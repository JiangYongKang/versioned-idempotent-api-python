"""并发上限治理：超限立即拒绝并说明原因，不无限排队也不静默丢弃。"""

from __future__ import annotations

import threading
import time

import pytest

from app.errors import ConcurrencyLimitExceededError
from app.service import OrderService


def _hold_request(service: OrderService, key: str) -> None:
    def run() -> None:
        service.create_order(
            version_raw="v1", idempotency_key=key,
            payload={"item": "a", "quantity": 1}, request_id=f"req-{key}",
        )

    threading.Thread(target=run, daemon=True).start()


def test_over_limit_rejected_immediately(service, caplog) -> None:
    """在途数超上限：立即拒绝，错误原因可区分，不排队不丢单。"""
    service.warehouse.delay = 0.5  # 夹具 max_in_flight=2
    _hold_request(service, "hold-0")
    _hold_request(service, "hold-1")
    time.sleep(0.15)  # 等两个请求进入在途态
    assert service.store.in_flight() == 2

    t0 = time.monotonic()
    with pytest.raises(ConcurrencyLimitExceededError) as ei:
        service.create_order(
            version_raw="v1", idempotency_key="overflow",
            payload={"item": "b", "quantity": 1}, request_id="overflow-req",
        )
    elapsed = time.monotonic() - t0

    # 立即拒绝而不是排队等待
    assert elapsed < 0.1
    assert ei.value.code.value == "concurrency_limit_exceeded"
    assert ei.value.retryable is True
    assert "limit 2" in ei.value.message

    # 被拒绝的请求没有任何副作用；在途的两个占用者随后正常完成
    assert service.warehouse.count() == 0  # 此刻仍在处理中，尚未写入
    # 判定日志可按标识/依据解释
    rec = next(r for r in caplog.records
               if getattr(r, "request_id", None) == "overflow-req"
               and getattr(r, "decision", None) == "REJECT")
    assert rec.levelname == "WARNING"
    assert getattr(rec, "decision") == "REJECT"
    assert "concurrency_limit_exceeded" in rec.getMessage()

    time.sleep(0.5)
    assert service.order_count() == 2
    assert service.warehouse.count() == 2

    # 名额释放后新请求可成功（没有永久占用 / 丢名额）
    r, rep = service.create_order(
        version_raw="v1", idempotency_key="after-drain",
        payload={"item": "c", "quantity": 1}, request_id="after-req",
    )
    assert rep is False and r.order_id


def test_rejected_then_retry_succeeds() -> None:
    """被限流的请求是 retryable 的；待名额释放后用同一幂等键重试成功。"""
    svc = OrderService(max_in_flight=1, request_timeout=2.0)
    svc.warehouse.delay = 0.3

    def run_holder() -> None:
        svc.create_order(version_raw="v1", idempotency_key="holder",
                         payload={"item": "x", "quantity": 1},
                         request_id="holder")

    threading.Thread(target=run_holder).start()
    time.sleep(0.1)

    # 第一次：超限被立即拒绝（此刻没有产生结论，键可安全复用）
    with pytest.raises(ConcurrencyLimitExceededError) as ei:
        svc.create_order(version_raw="v1", idempotency_key="retry-k",
                         payload={"item": "y", "quantity": 1},
                         request_id="retry-1")
    assert ei.value.retryable is True
    assert svc.order_count() == 0

    # 待在途清空后重试：成功，且没有重复写入
    time.sleep(0.35)
    r, rep = svc.create_order(version_raw="v1", idempotency_key="retry-k",
                              payload={"item": "y", "quantity": 1},
                              request_id="retry-2")
    assert rep is False and r.order_id
    assert svc.order_count() == 2
