"""核心服务层：编排版本解析、幂等、并发、超时、依赖调用与本地状态写入。

不依赖 HTTP，因此可通过函数调用、HTTP、CLI 等多种入口复用同一套结论：
同一输入在任何入口下得到的成功数据 / 错误码 / 判定依据都一致。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from . import errors as err
from .concurrency import ConcurrencyLimiter, ShutdownGate
from .dependencies import (
    MockInventoryService,
    MockPaymentGateway,
    UpstreamConfig,
    UpstreamError,
)
from .idempotency import BeginMode, IdempotencyStore
from .logging_setup import get_logger, log_decision
from .models import OrderView
from .versioning import resolve_version, validate_payload


@dataclass
class GovernanceConfig:
    max_inflight: int = 8
    request_timeout: float = 3.0
    shutdown_grace: float = 5.0


@dataclass
class RequestContext:
    request_id: str
    idempotency_key: str | None = None
    timeout: float | None = None
    # 故障注入：{"payment": {"delay": 0.2, "fail": true}, ...}
    injected: dict[str, dict[str, Any]] = field(default_factory=dict)
    entrypoint: str = "service"


def new_request_id() -> str:
    return "req_" + uuid.uuid4().hex[:12]


def _fingerprint(version: str, payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"version": version, "payload": payload},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _deterministic_id(prefix: str, key: str) -> str:
    return prefix + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


class OrderService:
    def __init__(self, config: GovernanceConfig | None = None,
                 logger: logging.Logger | None = None,
                 payment: MockPaymentGateway | None = None,
                 inventory: MockInventoryService | None = None) -> None:
        self.config = config or GovernanceConfig()
        self.log = logger or get_logger("service")
        self.idem = IdempotencyStore()
        self.limiter = ConcurrencyLimiter(self.config.max_inflight)
        self.gate = ShutdownGate(self.config.shutdown_grace)
        self.payment = payment or MockPaymentGateway()
        self.inventory = inventory or MockInventoryService()
        self._orders: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ #
    # 核心入口
    # ------------------------------------------------------------------ #
    async def create_order(
        self,
        raw_body: dict[str, Any] | bytes | str | None,
        ctx: RequestContext,
        path_version: str | None = None,
        header_version: str | None = None,
    ) -> dict[str, Any]:
        """创建订单的唯一核心入口，返回归一化响应信封。"""
        key = ctx.idempotency_key
        try:
            # 1) 版本协商（早于任何状态登记）
            from .versioning import parse_body
            version = resolve_version(path_version, header_version)
            body = parse_body(raw_body)
            payload = validate_payload(version, body)
            log_decision(
                self.log, request_id=ctx.request_id, idempotency_key=key,
                decision="version_resolved",
                reason=f"resolved version {version}; payload validated",
                version=version, entrypoint=ctx.entrypoint,
            )

            # 2) 幂等键必须存在（变更类请求）
            if not key:
                raise err.idempotency_key_missing()

            # 3) 关停闸门 -> 在途上限：拒绝必须发生在副作用之前
            await self.gate.enter()
            try:
                async with self.limiter.slot():
                    return await self._idempotent_create(version, payload, ctx)
            finally:
                await self.gate.leave()
        except err.AppError as e:
            log_decision(
                self.log, request_id=ctx.request_id, idempotency_key=key,
                decision="rejected", reason=e.reason,
                code=e.code, level=logging.WARNING, entrypoint=ctx.entrypoint,
            )
            return self._envelope_error(ctx, e)

    async def _idempotent_create(self, version: str, payload: dict[str, Any],
                                 ctx: RequestContext) -> dict[str, Any]:
        key = ctx.idempotency_key or ""
        fingerprint = _fingerprint(version, payload)
        record, mode = await self.idem.begin(key, fingerprint)

        if mode is BeginMode.FIRST:
            outcome = await self._run_once(version, payload, ctx)
            await self.idem.complete(key, outcome)
            return outcome

        # 在途或已完成：等待后重放完全一致的结论，自身不执行副作用
        if mode is BeginMode.WAIT:
            log_decision(
                self.log, request_id=ctx.request_id, idempotency_key=key,
                decision="idem_inflight_wait",
                reason="identical idempotency key in flight; waiting for "
                       "the original outcome",
            )
            try:
                await self.idem.wait_until_done(record)
            except asyncio.CancelledError:
                # 等待者自身被取消：它没有执行任何副作用，共享结论不受影响；
                # 对它自己的调用方仍返回可区分的 CANCELLED，不透出内部异常。
                e = err.cancelled_error("waiting_for_original")
                log_decision(
                    self.log, request_id=ctx.request_id,
                    idempotency_key=key, decision="cancelled",
                    reason=e.reason, code=e.code, level=logging.WARNING,
                )
                return self._envelope_error(ctx, e)
        else:
            log_decision(
                self.log, request_id=ctx.request_id, idempotency_key=key,
                decision="idem_replay",
                reason=f"replaying cached {record.status.value} outcome; "
                       "no side effect is executed again",
            )
        return self._as_replay(ctx, record)

    async def _run_once(self, version: str, payload: dict[str, Any],
                        ctx: RequestContext) -> dict[str, Any]:
        """真正执行副作用的唯一位置；同一幂等键只可能进入这里一次。"""
        key = ctx.idempotency_key or ""
        timeout = ctx.timeout
        if timeout is None:
            timeout = self.config.request_timeout
        log_decision(
            self.log, request_id=ctx.request_id, idempotency_key=key,
            decision="idem_first",
            reason="first submission for this idempotency key; executing "
                   "side effects exactly once",
        )
        try:
            charge, reservation = await asyncio.wait_for(
                self._call_dependencies(payload, ctx),
                timeout=timeout,
            )
        except TimeoutError:
            e = err.timeout_error("upstream", timeout)
            log_decision(
                self.log, request_id=ctx.request_id, idempotency_key=key,
                decision="timeout", reason=e.reason, code=e.code,
                level=logging.WARNING,
            )
            return self._envelope_error(ctx, e)
        except asyncio.CancelledError:
            # 取消只能发生在依赖调用阶段（该阶段含 await 挂起点）。
            e = err.cancelled_error("upstream")
            log_decision(
                self.log, request_id=ctx.request_id, idempotency_key=key,
                decision="cancelled", reason=e.reason, code=e.code,
                level=logging.WARNING,
            )
            # 归一化为可区分错误并落定确定结论；不 re-raise，避免内部
            # CancelledError 透出。依赖阶段尚未走完，订单不会写入。
            return self._envelope_error(ctx, e)
        except UpstreamError as exc:
            e = err.upstream_error(exc.dependency, exc.category)
            log_decision(
                self.log, request_id=ctx.request_id, idempotency_key=key,
                decision="upstream_failed", reason=e.reason, code=e.code,
                level=logging.WARNING,
            )
            return self._envelope_error(ctx, e)
        except err.AppError as e:
            log_decision(
                self.log, request_id=ctx.request_id, idempotency_key=key,
                decision="rejected", reason=e.reason, code=e.code,
                level=logging.WARNING,
            )
            return self._envelope_error(ctx, e)
        except Exception:  # noqa: BLE001 - 归一化兜底，不透出内部异常
            self.log.exception(
                "unexpected internal error",
                extra={"gov": {"request_id": ctx.request_id,
                               "idempotency_key": key}},
            )
            return self._envelope_error(ctx, err.internal_error())

        # 依赖全部成功之后才提交；提交是纯同步（无 await），不会再被取消或
        # 超时打断，因此“成功依赖 -> 唯一提交 -> 成功结论”是原子的。
        data = self._commit(version, payload, key, charge, reservation)
        log_decision(
            self.log, request_id=ctx.request_id, idempotency_key=key,
            decision="committed",
            reason="upstream calls succeeded and order committed exactly "
                   "once",
            order_id=data["order_id"],
        )
        return self._envelope_ok(ctx, data)

    async def _call_dependencies(self, payload: dict[str, Any],
                                 ctx: RequestContext) -> tuple[dict, dict]:
        """调用两个外部依赖；失败/超时/取消都发生在这里，且尚未写本地状态。"""
        key = ctx.idempotency_key or ""
        pay_cfg = UpstreamConfig.from_injection(ctx.injected, "payment")
        inv_cfg = UpstreamConfig.from_injection(ctx.injected, "inventory")
        charge = await self.payment.charge(
            _deterministic_id("ch_", key), payload["amount_cents"], pay_cfg,
        )
        reservation = await self.inventory.reserve(
            _deterministic_id("rsv_", key), payload["sku"], inv_cfg,
        )
        return charge, reservation

    def _commit(self, version: str, payload: dict[str, Any], key: str,
                charge: dict, reservation: dict) -> dict[str, Any]:
        """同步提交本地状态：幂等号决定 order_id，天然不会产生重复写入。"""
        record = {
            "order_id": _deterministic_id("ord_", key),
            "version": version,
            "status": "created",
            "customer_id": payload["customer_id"],
            "sku": payload["sku"],
            "quantity": payload["quantity"],
            "amount_cents": payload["amount_cents"],
            "note": payload["note"],
            "priority": payload.get("priority", "normal"),
            "coupon_code": payload.get("coupon_code"),
            "charge_id": charge["charge_id"],
            "reservation_id": reservation["reservation_id"],
        }
        self._orders[record["order_id"]] = record
        return OrderView.from_record(record, version).to_version_dict(version)

    # ------------------------------------------------------------------ #
    # 读接口
    # ------------------------------------------------------------------ #
    async def get_order(self, order_id: str, ctx: RequestContext,
                        path_version: str | None = None,
                        header_version: str | None = None) -> dict[str, Any]:
        try:
            version = resolve_version(path_version, header_version)
            record = self._orders.get(order_id)
            if record is None:
                raise err.not_found("order", order_id)
            data = OrderView.from_record(record, version).to_version_dict(version)
            log_decision(
                self.log, request_id=ctx.request_id,
                idempotency_key=ctx.idempotency_key,
                decision="read", reason="committed order returned",
                order_id=order_id, version=version,
            )
            return self._envelope_ok(ctx, data)
        except err.AppError as e:
            return self._envelope_error(ctx, e)

    # ------------------------------------------------------------------ #
    # 优雅关停
    # ------------------------------------------------------------------ #
    async def shutdown(self) -> bool:
        """发起优雅关停：拒绝新请求，等待在途请求结束。返回是否完全排空。"""
        await self.gate.begin_shutdown()
        log_decision(
            self.log, request_id="-", idempotency_key=None,
            decision="shutdown_begin",
            reason="shutdown signaled; new requests will be rejected; "
                   f"waiting for {self.gate.inflight} in-flight requests",
        )
        drained = await self.gate.wait_drained(self.config.shutdown_grace)
        log_decision(
            self.log, request_id="-", idempotency_key=None,
            decision="shutdown_complete",
            reason=("all in-flight requests settled" if drained
                    else "grace period elapsed; remaining requests must be "
                         "treated as failed/cancelled"),
            drained=drained,
            level=(logging.INFO if drained else logging.WARNING),
        )
        return drained

    # ------------------------------------------------------------------ #
    # 测试 / 观测辅助
    # ------------------------------------------------------------------ #
    def order_count(self) -> int:
        return len(self._orders)

    def reset(self) -> None:
        self.idem.reset()
        self._orders.clear()

    # ------------------------------------------------------------------ #
    # 信封
    # ------------------------------------------------------------------ #
    def _envelope_ok(self, ctx: RequestContext, data: dict[str, Any],
                     replayed: bool = False) -> dict[str, Any]:
        return {
            "ok": True,
            "data": data,
            "request_id": ctx.request_id,
            "idempotency_key": ctx.idempotency_key,
            "replayed": replayed,
        }

    def _envelope_error(self, ctx: RequestContext,
                        e: err.AppError, replayed: bool = False) -> dict[str, Any]:
        body = e.to_body()
        body["request_id"] = ctx.request_id
        body["idempotency_key"] = ctx.idempotency_key
        body["replayed"] = replayed
        body["http_status"] = e.http_status
        return body

    def _as_replay(self, ctx: RequestContext, record) -> dict[str, Any]:
        outcome = dict(record.outcome or {})
        outcome["request_id"] = ctx.request_id
        outcome["replayed"] = True
        return outcome
