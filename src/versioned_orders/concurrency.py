"""在途并发治理：全局信号量（立即拒绝，不排队）与关停闸门。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from .errors import concurrency_limit_reached, shutdown_error


class ConcurrencyLimiter:
    """全局在途上限：超过上限立即拒绝（CONCURRENCY_LIMIT），不排队。

    用条件变量手工记账而不是信号量：被拒绝者根本不占名额，也不会
    在信号量队列里等待——语义上是“立即拒绝而非无限排队”。
    """

    def __init__(self, max_inflight: int) -> None:
        if max_inflight < 1:
            raise ValueError("max_inflight must be >= 1")
        self.max_inflight = max_inflight
        self._inflight = 0
        self._cond = asyncio.Condition()

    @property
    def inflight(self) -> int:
        return self._inflight

    @asynccontextmanager
    async def slot(self):
        async with self._cond:
            if self._inflight >= self.max_inflight:
                raise concurrency_limit_reached(self.max_inflight)
            self._inflight += 1
        try:
            yield
        finally:
            async with self._cond:
                self._inflight -= 1
                self._cond.notify_all()

    async def wait_empty(self) -> None:
        async with self._cond:
            await self._cond.wait_for(lambda: self._inflight == 0)


class ShutdownGate:
    """关停闸门。

    - 正常期：``enter`` 登记一个在途请求；
    - 收到关停信号后：``begin_shutdown`` 翻转标志，所有新请求立即收到
      SHUTDOWN；在途请求继续执行，由 ``wait_drained`` 等待其结束，
      超过宽限期仍未结束则返回 False（由上层明确失败，不留不确定结果）。
    """

    def __init__(self, shutdown_grace: float = 5.0) -> None:
        self.shutdown_grace = shutdown_grace
        self.shutting_down = False
        self._inflight = 0
        self._cond = asyncio.Condition()

    async def enter(self) -> None:
        async with self._cond:
            if self.shutting_down:
                raise shutdown_error()
            self._inflight += 1

    async def leave(self) -> None:
        async with self._cond:
            self._inflight -= 1
            self._cond.notify_all()

    async def begin_shutdown(self) -> None:
        async with self._cond:
            self.shutting_down = True
            self._cond.notify_all()

    @property
    def inflight(self) -> int:
        return self._inflight

    async def wait_drained(self, timeout: float | None = None) -> bool:
        timeout = self.shutdown_grace if timeout is None else timeout
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        async with self._cond:
            while self._inflight > 0:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return False
                try:
                    await asyncio.wait_for(
                        self._cond.wait_for(lambda: self._inflight == 0),
                        timeout=remaining,
                    )
                except TimeoutError:
                    return False
            return True

    @asynccontextmanager
    async def track(self):
        """enter/leave 成对使用的便捷上下文。"""
        await self.enter()
        try:
            yield
        finally:
            await self.leave()
