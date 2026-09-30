"""核心订单服务：串起版本协商、幂等/并发治理、外部依赖与归一化错误。

同一套核心逻辑同时服务 HTTP 入口与进程内直调入口（tests / CLI），
保证不同入口下结论一致、可解释。

执行与失败语义：
- 校验顺序固定：版本 -> 幂等键 -> 字段 -> 并发/关停闸门 -> 幂等登记，
  每一步的拒绝原因互不相同、可区分。
- 只有幂等 leader 才会触发外部副作用；follower 阻塞等待同一结论。
- 外部调用在工作线程内执行，主线程以「先到结论先判定」的方式
  在 成功 / 调用方取消 / 超时 之间做确定性裁决：
    * 成功先到 -> 提交；
    * 取消先到 -> CancelledError；
    * 超时先到 -> TimeoutError；
  超时或取消都会令外部写入中止，或在竞态写入后立即回滚，
  因而不存在半成品中间状态。
- 任何未知内部异常都收敛成 INTERNAL_ERROR，绝不透出原生异常。
"""

from __future__ import annotations

import itertools
import json
import logging
import threading
from typing import Any

from app.errors import (
    CancelledError as ServiceCancelledError,
    ErrorCode,
    ExternalDependencyError,
    IdempotencyKeyMissingError,
    InternalError,
    ServiceError,
    TimeoutError as ServiceTimeoutError,
)
from app.external import ExternalCancelled, FakeWarehouse
from app.idempotency import IdempotencyStore, KeyStatus
from app.logging_setup import get_logger, log_decision
from app.models import OrderResult, build_response, parse_payload
from app.versioning import ApiVersion, resolve_version

# follower 重放失败结论时，错误码 -> 对外异常类型
_ERROR_CLASS: dict[str, type[ServiceError]] = {
    ErrorCode.TIMEOUT.value: ServiceTimeoutError,
    ErrorCode.CANCELLED.value: ServiceCancelledError,
    ErrorCode.EXTERNAL_DEPENDENCY_FAILED.value: ExternalDependencyError,
    ErrorCode.INTERNAL_ERROR.value: InternalError,
}

_request_seq = itertools.count(1)


def _new_request_id() -> str:
    # 入口（如 HTTP）若已设置上下文标识则沿用，否则进程内生成
    from app.context import get_request_id
    ctx = get_request_id()
    if ctx != "-":
        return ctx
    return f"req-{next(_request_seq)}"


class OrderService:
    def __init__(
        self,
        *,
        max_in_flight: int = 4,
        request_timeout: float = 2.0,
        warehouse: FakeWarehouse | None = None,
    ) -> None:
        self.warehouse = warehouse or FakeWarehouse()
        self.store = IdempotencyStore(max_in_flight=max_in_flight)
        self.request_timeout = request_timeout
        self._orders_lock = threading.Lock()
        self._orders: dict[str, dict[str, Any]] = {}
        self._order_seq = itertools.count(1)
        self._log = get_logger()

    # ------------------------------------------------------------------
    def create_order(
        self,
        *,
        version_raw: str | None,
        idempotency_key: str | None,
        payload: dict[str, Any],
        request_id: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> tuple[OrderResult, bool]:
        """创建订单，返回 ``(result, replayed)``。

        replayed=True 表示命中既有结论，本次无副作用。
        所有失败都以 ServiceError 子类抛出，错误码可区分。
        """
        req_id = request_id or _new_request_id()
        caller_cancel = cancel_event or threading.Event()

        # 1) 版本协商（缺失 / 不支持 可区分）
        version = resolve_version(version_raw)
        log_decision(self._log, request_id=req_id, idempotency_key=None,
                     decision="VERSION_RESOLVED", reason="header accepted",
                     version=version.value)

        # 2) 幂等键必须存在
        if not idempotency_key or not str(idempotency_key).strip():
            log_decision(self._log, request_id=req_id, idempotency_key=None,
                         decision="REJECT", reason="missing idempotency key",
                         level=logging.WARNING)
            raise IdempotencyKeyMissingError(
                "request header Idempotency-Key is required", request_id=req_id
            )
        key = str(idempotency_key).strip()

        # 3) 按版本严格校验字段
        order_req = parse_payload(version, payload if isinstance(payload, dict) else {})
        fingerprint = json.dumps(
            {
                "version": version.value,
                "item": order_req.item,
                "quantity": order_req.quantity,
                "priority": order_req.priority,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

        # 4) 关停 / 在途闸门（立即拒绝，不排队）
        try:
            self.store.try_enter()
        except ServiceError as exc:
            log_decision(
                self._log, request_id=req_id, idempotency_key=key,
                decision="REJECT", reason=exc.code.value,
                level=logging.WARNING, in_flight=self.store.in_flight(),
                limit=self.store.max_in_flight,
            )
            exc.request_id = req_id
            raise
        log_decision(self._log, request_id=req_id, idempotency_key=key,
                     decision="ADMITTED", reason="capacity available",
                     in_flight=self.store.in_flight(),
                     limit=self.store.max_in_flight)
        try:
            # 5) 幂等登记：leader 执行，follower 等待同一结论
            state, is_leader = self.store.acquire_or_join(
                key, fingerprint, caller_cancel
            )
            if not is_leader:
                return self._replay(req_id, key, version, state)

            # 6) leader：有界执行
            return self._execute(
                req_id=req_id, key=key, version=version,
                item=order_req.item, quantity=order_req.quantity,
                priority=order_req.priority, fingerprint=fingerprint,
                caller_cancel=caller_cancel,
            )
        finally:
            self.store.leave()

    # ------------------------------------------------------------------
    def _replay(self, req_id: str, key: str, version: ApiVersion, state) -> tuple[OrderResult, bool]:
        if state.status is KeyStatus.COMPLETED:
            log_decision(self._log, request_id=req_id, idempotency_key=key,
                         decision="REPLAY", reason="key already completed",
                         order_id=state.result.order_id)
            return state.result, True

        # 失败结论同样一致地重放给同键后来者
        code = state.error_code or ErrorCode.INTERNAL_ERROR.value
        message = state.error_message or "previous attempt failed"
        log_decision(self._log, request_id=req_id, idempotency_key=key,
                     decision="REPLAY_FAILURE", reason="key already failed",
                     error_code=code)
        exc_cls = _ERROR_CLASS.get(code, InternalError)
        raise exc_cls(f"{message} (replayed for idempotency key {key!r})",
                      request_id=req_id)

    # ------------------------------------------------------------------
    def _execute(self, *, req_id: str, key: str, version: ApiVersion,
                 item: str, quantity: int, priority: str,
                 fingerprint: str,
                 caller_cancel: threading.Event) -> tuple[OrderResult, bool]:
        abort = threading.Event()
        done = threading.Event()
        box: dict[str, Any] = {}

        def _abort_watch() -> None:
            if caller_cancel.wait(self.request_timeout):
                abort.set()  # 调用方取消
            else:
                abort.set()  # 超时

        watcher = threading.Thread(target=_abort_watch, name=f"watch-{req_id}",
                                   daemon=True)
        watcher.start()

        def _work() -> None:
            try:
                reservation_id = self.warehouse.reserve(
                    key, item, quantity, abort
                )
                # 写入后再次确认：若超时/取消在临界时刻到达，则回滚外部写入
                if abort.is_set():
                    self.warehouse.release(key)
                    box["outcome"] = "aborted"
                    return
                order_id = f"ord-{next(self._order_seq)}"
                record = {
                    "order_id": order_id,
                    "reservation_id": reservation_id,
                    "item": item,
                    "quantity": quantity,
                    "priority": priority,
                    "api_version": version.value,
                    "idempotency_key": key,
                }
                with self._orders_lock:
                    self._orders[order_id] = record
                box["outcome"] = "success"
                box["result"] = OrderResult(
                    order_id, item, quantity, priority, version.value
                )
            except ExternalCancelled:
                box["outcome"] = "aborted"
            except ExternalDependencyError as exc:
                box["outcome"] = "external_error"
                box["message"] = exc.message
            except Exception as exc:  # 归一化：内部异常不透出
                box["outcome"] = "internal_error"
                box["message"] = repr(exc)
            finally:
                done.set()

        worker = threading.Thread(target=_work, name=f"work-{req_id}", daemon=True)
        worker.start()

        # 有界等待：worker 是提交的权威裁决方（reserve 后复查 abort）。
        # 超时窗口用尽不直接判失败，而是触发 abort 并等待 worker 完成
        # 中止/回滚收尾，再统一以 worker 的 outcome 为准——杜绝“已落库却记失败”。
        timed_out = False
        if not done.wait(self.request_timeout):
            timed_out = True
            abort.set()  # 兜底置位（watcher 也会置，幂等）
            worker.join()

        outcome = box.get("outcome")
        if outcome == "success":
            result: OrderResult = box["result"]
            self.store.succeed(key, result)
            log_decision(self._log, request_id=req_id, idempotency_key=key,
                         decision="COMMIT", reason="single side effect",
                         order_id=result.order_id,
                         reservation=self.warehouse.count())
            return result, False

        # 工作已结束但属于中止 / 外部失败 / 内部失败
        if outcome == "aborted":
            # 调用方取消优先归类；否则（超时窗口用尽、未提交）归为超时
            if caller_cancel.is_set():
                error: ServiceError = ServiceCancelledError(
                    "request cancelled by caller before completion",
                    request_id=req_id,
                )
                code = ErrorCode.CANCELLED.value
                level = logging.WARNING
            else:
                error = ServiceTimeoutError(
                    f"request timed out after {self.request_timeout}s",
                    request_id=req_id,
                )
                code = ErrorCode.TIMEOUT.value
                level = logging.WARNING
            log_decision(self._log, request_id=req_id, idempotency_key=key,
                         decision="ABORT", reason=code, level=level)
            self.store.fail(key, code, error.message)
            raise error

        if outcome == "external_error":
            err = ExternalDependencyError(
                f"external dependency failure: {box.get('message', 'unknown')}",
                request_id=req_id,
            )
            log_decision(self._log, request_id=req_id, idempotency_key=key,
                         decision="FAIL", reason="external dependency",
                         level=logging.WARNING)
            self.store.fail(key, ErrorCode.EXTERNAL_DEPENDENCY_FAILED.value,
                            err.message)
            raise err

        err = InternalError(
            "unexpected internal error; request failed safely",
            request_id=req_id,
        )
        log_decision(self._log, request_id=req_id, idempotency_key=key,
                     decision="FAIL",
                     reason=f"internal error: {box.get('message', '?')}",
                     level=logging.ERROR)
        self.store.fail(key, ErrorCode.INTERNAL_ERROR.value, err.message)
        raise err


    # ------------------------------------------------------------------
    def shutdown(self, timeout: float | None = 10.0) -> bool:
        """优雅关停：拒绝新请求，等待在途请求得出确定结论。"""
        self.store.begin_shutdown()
        drained = self.store.wait_drained(timeout)
        log_decision(self._log, request_id="-", idempotency_key=None,
                     decision="SHUTDOWN",
                     reason="drained" if drained else "drain timeout",
                     level=logging.INFO if drained else logging.ERROR)
        return drained

    # ---- 供入口 / 测试使用 ----
    def to_response(self, result: OrderResult, version: ApiVersion) -> dict:
        return build_response(result, version.value)

    def order_count(self) -> int:
        with self._orders_lock:
            return len(self._orders)
