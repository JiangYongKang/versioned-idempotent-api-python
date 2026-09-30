"""FastAPI 入口层：把 HTTP 请求映射到核心服务，并归一化错误。

路由（三种版本入口，结论完全一致）：
- ``POST /v1/orders``、``POST /v2/orders``      版本由路径前缀给出
- ``POST /orders`` + ``X-API-Version`` 头        版本由头给出（缺失即拒绝）
- ``GET  /v1/orders/{id}``、``/v2/orders/{id}`` 按版本裁剪返回结构
- ``GET  /orders/{id}`` + ``X-API-Version`` 头

变更请求必须带 ``Idempotency-Key`` 头。
仅用于本地验证的测试钩子：``X-Debug-Inject``（故障注入 JSON）、
``X-Request-Timeout``（覆盖本次超时预算，秒）。
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .errors import AppError, internal_error
from .logging_setup import configure_logging, get_logger, log_decision
from .service import (
    GovernanceConfig,
    OrderService,
    RequestContext,
    new_request_id,
)
from .versioning import header_name

IDEMPOTENCY_HEADER = "idempotency-key"
REQUEST_ID_HEADER = "x-request-id"
INJECT_HEADER = "x-debug-inject"
TIMEOUT_HEADER = "x-request-timeout"


def create_app(service: OrderService | None = None,
               config: GovernanceConfig | None = None) -> FastAPI:
    configure_logging()
    log = get_logger("api")
    service = service or OrderService(config or GovernanceConfig())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        log_decision(log, request_id="-", idempotency_key=None,
                     decision="startup", reason="service ready")
        try:
            yield
        finally:
            # 优雅关停：拒绝新请求，在途请求处理完或宽限期后明确失败
            await service.shutdown()

    app = FastAPI(
        title="Versioned Idempotent Orders API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.service = service

    def _build_ctx(request: Request) -> RequestContext:
        request_id = request.headers.get(REQUEST_ID_HEADER) or new_request_id()
        injected: dict = {}
        raw_inject = request.headers.get(INJECT_HEADER)
        if raw_inject:
            try:
                parsed = json.loads(raw_inject)
                if isinstance(parsed, dict):
                    injected = parsed
            except json.JSONDecodeError:
                injected = {}
        timeout: float | None = None
        raw_timeout = request.headers.get(TIMEOUT_HEADER)
        if raw_timeout:
            try:
                timeout = float(raw_timeout)
            except ValueError:
                timeout = None
        return RequestContext(
            request_id=request_id,
            idempotency_key=request.headers.get(IDEMPOTENCY_HEADER),
            timeout=timeout,
            injected=injected,
            entrypoint="http",
        )

    def _reply(outcome: dict) -> JSONResponse:
        # 用副本剥离传输字段，绝不修改可能被幂等存储缓存的信封
        body = dict(outcome)
        status = int(body.pop("http_status", 200))
        return JSONResponse(status_code=status, content=body)

    async def _create(request: Request, path_version: str | None) -> JSONResponse:
        ctx = _build_ctx(request)
        raw = await request.body()
        outcome = await service.create_order(
            raw, ctx,
            path_version=path_version,
            header_version=request.headers.get(header_name()),
        )
        return _reply(outcome)

    async def _read(request: Request, order_id: str,
                    path_version: str | None) -> JSONResponse:
        ctx = _build_ctx(request)
        outcome = await service.get_order(
            order_id, ctx,
            path_version=path_version,
            header_version=request.headers.get(header_name()),
        )
        return _reply(outcome)

    @app.post("/orders")
    async def create_generic(request: Request) -> JSONResponse:
        return await _create(request, None)

    @app.post("/v1/orders")
    async def create_v1(request: Request) -> JSONResponse:
        return await _create(request, "v1")

    @app.post("/v2/orders")
    async def create_v2(request: Request) -> JSONResponse:
        return await _create(request, "v2")

    @app.get("/orders/{order_id}")
    async def read_generic(request: Request, order_id: str) -> JSONResponse:
        return await _read(request, order_id, None)

    @app.get("/v1/orders/{order_id}")
    async def read_v1(request: Request, order_id: str) -> JSONResponse:
        return await _read(request, order_id, "v1")

    @app.get("/v2/orders/{order_id}")
    async def read_v2(request: Request, order_id: str) -> JSONResponse:
        return await _read(request, order_id, "v2")

    # 形如 /v3/orders 的“带版本前缀但不支持”的请求：给出可区分的版本错误，
    # 而不是落进不可解释的通用 404
    @app.post("/{ver}/orders")
    async def create_unknown_version(request: Request, ver: str) -> JSONResponse:
        ctx = _build_ctx(request)
        raw = await request.body()
        outcome = await service.create_order(
            raw, ctx, path_version=ver,
            header_version=request.headers.get(header_name()),
        )
        return _reply(outcome)

    @app.get("/{ver}/orders/{order_id}")
    async def read_unknown_version(request: Request, ver: str,
                                   order_id: str) -> JSONResponse:
        ctx = _build_ctx(request)
        outcome = await service.get_order(
            order_id, ctx, path_version=ver,
            header_version=request.headers.get(header_name()),
        )
        return _reply(outcome)

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True, "shutting_down": service.gate.shutting_down,
                "inflight": service.gate.inflight}

    @app.get("/")
    async def index() -> dict:
        return {
            "service": "versioned-idempotent-orders",
            "supported_versions": ["v1", "v2"],
            "endpoints": [
                "POST /v1/orders", "POST /v2/orders",
                "POST /orders (X-API-Version required)",
                "GET /v1/orders/{id}", "GET /v2/orders/{id}",
            ],
        }

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=exc.to_body())

    @app.exception_handler(Exception)
    async def fallback_handler(request: Request, exc: Exception) -> JSONResponse:
        # 任何未预期异常：日志留痕，对外只给固定 INTERNAL_ERROR
        request_id = request.headers.get(REQUEST_ID_HEADER, "-")
        log.log(logging.ERROR, "unhandled exception",
                exc_info=exc,
                extra={"gov": {"request_id": request_id,
                               "idempotency_key": "-", "decision": "panic",
                               "reason": "unexpected internal exception"}})
        error = internal_error()
        return JSONResponse(status_code=500, content=error.to_body())

    return app
