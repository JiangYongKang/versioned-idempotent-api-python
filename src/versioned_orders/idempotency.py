"""幂等键存储：去重、在途占用与结果重放。

语义（对所有入口一致）：
- 首次提交：键进入 INFLIGHT，调用方执行副作用，结束后落定唯一最终结论；
- 同键同请求指纹并发到达：后到者等待在途完成，拿到与首个请求完全一致的
  结果，自身不执行任何副作用；
- 同键但请求指纹不同（无论在途还是已完成）：IDEMPOTENCY_REPLAY_CONFLICT；
- 最终结论（成功或失败，含超时/取消/依赖失败）都会被缓存并重放，保证
  “同一个键永远得到同一个结论”；失败后若要重试，必须换用新幂等键。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum


class ReqStatus(str, Enum):
    INFLIGHT = "inflight"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class BeginMode(str, Enum):
    FIRST = "first"          # 首次：本次负责执行
    REPLAY = "replay"        # 已有完成结论：直接重放
    WAIT = "wait"            # 在途：等待后重放同一结论


@dataclass
class IdempotentRecord:
    key: str
    request_hash: str
    status: ReqStatus
    outcome: dict | None = None
    completed: asyncio.Event = field(default_factory=asyncio.Event)


class IdempotencyStore:
    def __init__(self) -> None:
        self._records: dict[str, IdempotentRecord] = {}
        self._guard = asyncio.Lock()        # 保护注册表本身
        self._key_locks: dict[str, asyncio.Lock] = {}

    async def begin(self, key: str,
                    request_hash: str) -> tuple[IdempotentRecord, BeginMode]:
        """登记幂等键。返回 (记录, 本次进入模式)。"""
        async with self._guard:
            lock = self._key_locks.setdefault(key, asyncio.Lock())
        async with lock:
            existing = self._records.get(key)
            if existing is None:
                record = IdempotentRecord(
                    key=key, request_hash=request_hash,
                    status=ReqStatus.INFLIGHT,
                )
                self._records[key] = record
                return record, BeginMode.FIRST
            if existing.request_hash != request_hash:
                # 同键绑定了不同请求体：明确拒绝，避免错误重放
                from .errors import replay_conflict
                raise replay_conflict(key)
            if existing.status is ReqStatus.INFLIGHT:
                return existing, BeginMode.WAIT
            return existing, BeginMode.REPLAY

    async def wait_until_done(self, record: IdempotentRecord) -> None:
        """等待在途请求落定（不在任何内部锁内等待）。"""
        await record.completed.wait()

    async def complete(self, key: str, outcome: dict) -> None:
        """落定一次请求的最终结论，并唤醒所有等待者（仅首次调用有效）。"""
        async with self._guard:
            lock = self._key_locks.get(key)
        if lock is None:
            return
        async with lock:
            record = self._records.get(key)
            if record is None or record.status is not ReqStatus.INFLIGHT:
                return
            record.status = (
                ReqStatus.SUCCEEDED if outcome.get("ok") else ReqStatus.FAILED
            )
            # 存副本：调用方后续对信封的修改（如剥离传输字段）不得污染缓存
            record.outcome = dict(outcome)
            record.completed.set()

    def get_status(self, key: str) -> ReqStatus | None:
        record = self._records.get(key)
        return None if record is None else record.status

    def snapshot(self) -> dict[str, str]:
        """供测试断言：键 -> 状态。"""
        return {k: v.status.value for k, v in self._records.items()}

    def reset(self) -> None:
        self._records.clear()
        self._key_locks.clear()
