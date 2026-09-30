"""本地状态模拟的外部依赖（“库存系统”）。

用线程锁保护的内存表模拟外部写入，支持注入延迟与强制失败，
用于复现超时、取消与外部依赖失败场景。

所有失败路径都保证：没有半成品写入。
"""

from __future__ import annotations

import threading
import time

from app.errors import ExternalDependencyError


class ExternalCancelled(Exception):
    """内部信号：外部调用途中被取消。

    不是对外错误类型；服务层必须将其归一化为 CancelledError，
    禁止直接透出到入口边界之外。
    """


class FakeWarehouse:
    """模拟外部库存预留。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reservations: dict[str, dict] = {}
        # 测试钩子：调用耗时（秒）/ 是否强制失败
        self.delay: float = 0.0
        self.force_fail: bool = False

    def reserve(
        self,
        reference: str,
        item: str,
        quantity: int,
        cancel_event: threading.Event | None = None,
    ) -> str:
        """执行一次预留，返回预留单号。

        - delay 期间以小步长等待，cancel_event 被置位则立即中断且不写入；
        - force_fail 为真时在“写入前”抛出 ExternalDependencyError，不写入；
        - 同一 reference 已预留时直接返回既有单号（外部自身幂等）。
        """
        # 外部自身的幂等：同引用不重复预留
        with self._lock:
            existing = self.reservations.get(reference)
        if existing is not None:
            return existing["reservation_id"]

        # 模拟外部处理耗时，期间可被取消
        deadline = time.monotonic() + max(self.delay, 0.0)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if cancel_event is not None and cancel_event.wait(min(remaining, 0.01)):
                # 被取消：什么都没写
                raise ExternalCancelled(
                    f"warehouse reserve aborted by cancellation for {reference!r}"
                )

        if self.force_fail:
            raise ExternalDependencyError(
                f"warehouse rejected reservation for {reference!r} (forced failure)"
            )

        reservation_id = f"rsv-{reference}"
        with self._lock:
            # double-check，保证引用唯一
            if reference in self.reservations:
                return self.reservations[reference]["reservation_id"]
            self.reservations[reference] = {
                "reservation_id": reservation_id,
                "item": item,
                "quantity": quantity,
            }
        return reservation_id

    def release(self, reference: str) -> None:
        """失败回滚：移除预留（不存在时为空操作）。"""
        with self._lock:
            self.reservations.pop(reference, None)

    def count(self) -> int:
        with self._lock:
            return len(self.reservations)

    def reset(self) -> None:
        with self._lock:
            self.reservations.clear()
            self.delay = 0.0
            self.force_fail = False
