"""超时、取消、外部依赖失败的归一化与状态一致性。"""

from __future__ import annotations

import threading
import time

import pytest

from app.errors import (
    CancelledError,
    ExternalDependencyError,
    TimeoutError,
)


def test_timeout_normalized(service) -> None:
    """外部依赖超过请求超时 -> TimeoutError，外部无残留写入。"""
    service.warehouse.delay = 5.0
    t0 = time.monotonic()
    with pytest.raises(TimeoutError) as ei:
        service.create_order(
            version_raw="v1", idempotency_key="slow",
            payload={"item": "a", "quantity": 1}, request_id="slow-req",
        )
    elapsed = time.monotonic() - t0
    # 约在超时点返回（不会等外部 5 秒）
    assert 0.9 <= elapsed < 2.0, elapsed
    assert ei.value.code.value == "timeout"
    assert ei.value.retryable is True
    # 无中间状态
    assert service.order_count() == 0
    # 给中止收尾一点时间后确认外部也无残留
    time.sleep(0.1)
    assert service.warehouse.count() == 0


def test_timeout_conclusion_replayed(service) -> None:
    """超时结论对同键后来者一致重放，可区分仍是 timeout。"""
    service.warehouse.delay = 5.0
    with pytest.raises(TimeoutError):
        service.create_order(version_raw="v1", idempotency_key="slow2",
                             payload={"item": "a", "quantity": 1},
                             request_id="s1")
    # 外部已被快速失败：第二次即便外部不再慢，也拿到一致的失败结论
    service.warehouse.delay = 0.0
    with pytest.raises(TimeoutError) as ei:
        service.create_order(version_raw="v1", idempotency_key="slow2",
                             payload={"item": "a", "quantity": 1},
                             request_id="s2")
    assert ei.value.code.value == "timeout"
    assert "replayed" in ei.value.message


def test_cancel_midflight_normalized(service) -> None:
    """处理途中取消 -> CancelledError，外部无残留写入。"""
    service.warehouse.delay = 2.0
    cancel_event = threading.Event()
    outcome: dict = {}

    def run() -> None:
        try:
            service.create_order(
                version_raw="v1", idempotency_key="cancel-me",
                payload={"item": "a", "quantity": 1},
                request_id="cancel-req", cancel_event=cancel_event,
            )
        except Exception as exc:  # noqa: BLE001
            outcome["error"] = exc

    t = threading.Thread(target=run)
    t.start()
    time.sleep(0.15)
    cancel_event.set()
    t.join()

    err = outcome.get("error")
    assert isinstance(err, CancelledError), err
    assert err.code.value == "cancelled"
    assert err.code.value != "timeout"  # 两类错误可区分
    assert service.order_count() == 0
    time.sleep(0.1)
    assert service.warehouse.count() == 0


def test_external_failure_normalized_and_retryable(service) -> None:
    """外部依赖强制失败 -> ExternalDependencyError，可重试且无中间状态。"""
    service.warehouse.force_fail = True
    with pytest.raises(ExternalDependencyError) as ei:
        service.create_order(
            version_raw="v1", idempotency_key="boom",
            payload={"item": "a", "quantity": 1}, request_id="boom-req",
        )
    assert ei.value.code.value == "external_dependency_failed"
    assert ei.value.retryable is True
    assert service.order_count() == 0
    assert service.warehouse.count() == 0


def test_failed_key_recorded_new_key_recovers(service) -> None:
    """失败结论被记录；换新幂等键、外部恢复后可以成功。"""
    service.warehouse.force_fail = True
    with pytest.raises(ExternalDependencyError):
        service.create_order(version_raw="v1", idempotency_key="fail-k",
                             payload={"item": "a", "quantity": 1},
                             request_id="f1")
    service.warehouse.force_fail = False
    r, rep = service.create_order(version_raw="v1", idempotency_key="fresh-k",
                                  payload={"item": "a", "quantity": 1},
                                  request_id="f2")
    assert rep is False and r.order_id
    assert service.order_count() == 1
    assert service.warehouse.count() == 1


def test_no_raw_internal_exception_leaks(service, caplog) -> None:
    """外部依赖内部抛任意异常时，对外只得到归一化错误（内部错误类）。"""
    def boom(reference, item, quantity, cancel_event=None):  # noqa: ANN001
        raise RuntimeError("kaboom-internal-detail")

    service.warehouse.reserve = boom  # type: ignore[method-assign]
    from app.errors import InternalError

    with pytest.raises(InternalError) as ei:
        service.create_order(version_raw="v1", idempotency_key="raw",
                             payload={"item": "a", "quantity": 1},
                             request_id="raw-req")
    # 对外消息不泄露内部细节
    assert "kaboom-internal-detail" not in ei.value.message
    assert ei.value.code.value == "internal_error"
    # 内部细节仅留在判定日志里，便于排查
    assert any("kaboom-internal-detail" in r.getMessage()
               for r in caplog.records)
    assert service.order_count() == 0


def test_commit_just_at_timeout_boundary_is_consistent(monkeypatch) -> None:
    """外部恰好在超时点完成写入：以 worker 提交点为准，结果成功且状态一致。

    回归保护：杜绝“副作用已落库、幂等键却记为 timeout 失败”的中间状态。
    """
    from app.service import OrderService

    svc = OrderService(max_in_flight=2, request_timeout=0.3)

    # 外部耗时恰好略小于超时：在 abort 置位前完成 reserve 与提交复查
    svc.warehouse.delay = 0.25
    result, replayed = svc.create_order(
        version_raw="v1", idempotency_key="boundary",
        payload={"item": "a", "quantity": 1}, request_id="boundary-req",
    )
    assert replayed is False
    assert result.order_id
    assert svc.order_count() == 1
    assert svc.warehouse.count() == 1

    # 同键再来应命中成功重放，而不是 timeout
    again, replayed2 = svc.create_order(
        version_raw="v1", idempotency_key="boundary",
        payload={"item": "a", "quantity": 1}, request_id="boundary-req-2",
    )
    assert replayed2 is True
    assert again.order_id == result.order_id


def test_follower_cancelled_while_waiting_leader() -> None:
    """同键 follower 在等待在途 leader 期间被自身取消 -> cancelled，leader 不受影响。"""
    from app.service import OrderService

    svc = OrderService(max_in_flight=4, request_timeout=5.0)
    svc.warehouse.delay = 1.0
    leader_outcome = {}
    follower_outcome = {}
    leader_cancel = threading.Event()
    follower_cancel = threading.Event()

    def leader() -> None:
        r, _ = svc.create_order(
            version_raw="v1", idempotency_key="samek",
            payload={"item": "a", "quantity": 1}, request_id="L",
            cancel_event=leader_cancel,
        )
        leader_outcome["r"] = r

    def follower() -> None:
        try:
            svc.create_order(
                version_raw="v1", idempotency_key="samek",
                payload={"item": "a", "quantity": 1}, request_id="F",
                cancel_event=follower_cancel,
            )
        except Exception as exc:  # noqa: BLE001
            follower_outcome["e"] = exc

    tl = threading.Thread(target=leader)
    tl.start()
    time.sleep(0.15)
    tf = threading.Thread(target=follower)
    tf.start()
    time.sleep(0.15)
    follower_cancel.set()  # 只取消 follower
    tf.join()
    tl.join()

    assert isinstance(follower_outcome.get("e"), CancelledError)
    assert follower_outcome["e"].code.value == "cancelled"
    assert "r" in leader_outcome  # leader 照常成功
    assert svc.order_count() == 1
