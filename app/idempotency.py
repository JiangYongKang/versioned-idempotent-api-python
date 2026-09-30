"""幂等去重与并发治理。

- 每个幂等键有独立状态：PENDING -> COMPLETED / FAILED，由 Condition 通知。
- 同键并发：后到者（follower）等待首个请求（leader）的结论：
  * leader 成功 -> 回放同一结果，本次不产生任何副作用；
  * leader 失败 -> follower 收到同一错误码（结论一致、可区分）；
  * 请求体指纹不同 -> 立刻返回幂等冲突。
- 全局在途计数有明确上限，超限立即拒绝（不排队、不静默丢弃）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum

from app.errors import (
    CancelledError,
    ConcurrencyLimitExceededError,
    IdempotencyConflictError,
    ShuttingDownError,
)
from app.models import OrderResult


class KeyStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class KeyState:
    status: KeyStatus = KeyStatus.PENDING
    fingerprint: str = ""
    result: OrderResult | None = None
    error_code: str | None = None
    error_message: str | None = None
    cond: threading.Condition = field(default_factory=threading.Condition)


class IdempotencyStore:
    """幂等键状态表 + 全局并发闸门 + 关停信号。"""

    def __init__(self, max_in_flight: int = 4) -> None:
        if max_in_flight < 1:
            raise ValueError("max_in_flight must be >= 1")
        self.max_in_flight = max_in_flight
        self._states: dict[str, KeyState] = {}
        self._table_lock = threading.Lock()
        self._in_flight = 0
        self._drained = threading.Condition()
        self._shutting_down = threading.Event()

    # ---- 生命周期 / 关停 ----
    def begin_shutdown(self) -> None:
        """进入关停态：此后新请求一律被 :meth:`try_enter` 拒绝。"""
        self._shutting_down.set()

    @property
    def shutting_down(self) -> bool:
        return self._shutting_down.is_set()

    def in_flight(self) -> int:
        with self._drained:
            return self._in_flight

    def wait_drained(self, timeout: float | None = None) -> bool:
        """等待在途请求归零；超时返回 False。"""
        with self._drained:
            return self._drained.wait_for(
                lambda: self._in_flight == 0, timeout=timeout
            )

    # ---- 幂等登记 ----
    def acquire_or_join(
        self,
        key: str,
        fingerprint: str,
        cancel_event: threading.Event | None = None,
    ) -> tuple[KeyState, bool]:
        """登记幂等键。

        返回 ``(state, is_leader)``：
        - leader（True）：拿到执行权，最后必须调用 succeed/fail；
        - follower（False）：键已存在并已阻塞等待到结论，直接读取 state；
        - 与在途 / 已完成的同键请求指纹不同 -> IdempotencyConflictError；
        - follower 等待期间自身被取消 -> CancelledError（不改动既有结论）。
        """
        with self._table_lock:
            existing = self._states.get(key)
            if existing is None:
                state = KeyState(fingerprint=fingerprint)
                self._states[key] = state
                return state, True

        # 键已存在：先在不持表锁的情况下做指纹比对与等待
        with existing.cond:
            if existing.fingerprint != fingerprint:
                raise IdempotencyConflictError(
                    f"idempotency key {key!r} was already used with a different payload"
                )
            while existing.status is KeyStatus.PENDING:
                if cancel_event is not None and cancel_event.is_set():
                    raise CancelledError(
                        f"request cancelled while waiting on in-flight key {key!r}"
                    )
                existing.cond.wait(timeout=0.05)
            return existing, False

    def succeed(self, key: str, result: OrderResult) -> None:
        with self._table_lock:
            state = self._states[key]
        with state.cond:
            state.status = KeyStatus.COMPLETED
            state.result = result
            state.cond.notify_all()

    def fail(self, key: str, code: str, message: str) -> None:
        with self._table_lock:
            state = self._states[key]
        with state.cond:
            state.status = KeyStatus.FAILED
            state.error_code = code
            state.error_message = message
            state.cond.notify_all()

    # ---- 并发闸门 ----
    def try_enter(self) -> None:
        """占用一个在途名额。

        - 已进入关停态 -> ShuttingDownError；
        - 在途数已达上限 -> ConcurrencyLimitExceededError（立即拒绝）。
        成功后必须配对调用 :meth:`leave`。
        """
        with self._drained:
            if self._shutting_down.is_set():
                raise ShuttingDownError(
                    "service is shutting down; new requests are rejected"
                )
            if self._in_flight >= self.max_in_flight:
                raise ConcurrencyLimitExceededError(
                    f"in-flight limit {self.max_in_flight} exceeded; "
                    f"current={self._in_flight}"
                )
            self._in_flight += 1

    def leave(self) -> None:
        with self._drained:
            if self._in_flight > 0:
                self._in_flight -= 1
            if self._in_flight == 0:
                self._drained.notify_all()

    def reset(self) -> None:
        with self._table_lock, self._drained:
            self._states.clear()
            self._in_flight = 0
            self._shutting_down.clear()
            self._drained.notify_all()
