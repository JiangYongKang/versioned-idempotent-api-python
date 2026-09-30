"""优雅关停：在途请求处理完得出确定结论，新请求明确拒绝。"""

from __future__ import annotations

import threading
import time

import pytest

from app.errors import ShuttingDownError


def test_inflight_completes_on_shutdown(service) -> None:
    """关停后在途请求仍跑完，结果确定，不留未知状态。"""
    service.warehouse.delay = 0.3

    def run() -> None:
        service.create_order(
            version_raw="v1", idempotency_key="inflight",
            payload={"item": "a", "quantity": 1}, request_id="in-req",
        )

    t = threading.Thread(target=run)
    t.start()
    time.sleep(0.1)

    ok = service.shutdown(timeout=5.0)
    t.join()

    assert ok is True
    assert service.store.in_flight() == 0
    # 在途请求已被处理完，副作用确定存在
    assert service.order_count() == 1
    assert service.warehouse.count() == 1


def test_new_request_rejected_after_shutdown(service) -> None:
    service.shutdown(timeout=1.0)
    with pytest.raises(ShuttingDownError) as ei:
        service.create_order(
            version_raw="v1", idempotency_key="after",
            payload={"item": "a", "quantity": 1}, request_id="after-req",
        )
    assert ei.value.code.value == "shutting_down"
    assert service.order_count() == 0


def test_shutdown_with_failing_inflight_is_determined(service) -> None:
    """在途请求即便外部失败，关停也会等到它得出确定结论。"""
    service.warehouse.force_fail = True
    errors: list = []

    def run() -> None:
        try:
            service.create_order(
                version_raw="v1", idempotency_key="dying",
                payload={"item": "a", "quantity": 1}, request_id="d-req",
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=run)
    t.start()
    time.sleep(0.1)
    drained = service.shutdown(timeout=5.0)
    t.join()

    assert drained is True
    assert len(errors) == 1
    assert errors[0].code.value == "external_dependency_failed"
    assert service.store.in_flight() == 0
    assert service.order_count() == 0
