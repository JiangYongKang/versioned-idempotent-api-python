"""HTTP 入口（FastAPI）。

与进程内直调入口共享同一个 :class:`~app.service.OrderService`，
因此版本判定、幂等结论、并发拒绝、超时/取消语义完全一致。

约定：
- 版本：请求头 ``X-Api-Version``（v1 / v2）
- 幂等键：请求头 ``Idempotency-Key``
- 请求标识：请求头 ``X-Request-ID``（缺省自动生成）
- 命中既有结论时响应头 ``Idempotent-Replay: true``
- 成功与失败都带 ``X-Request-ID``，错误体为统一的
  ``{"error": {code, message, retryable, request_id}}`` 结构。
"""

from __future__ import annotations

import asyncio
import contextlib
import threading

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.context import set_request_id
from app.errors import InvalidFieldError, ServiceError
from app.logging_setup import configure_logging
from app.models import build_response
from app.service import OrderService

# ServiceError 子类 -> HTTP 状态码（响应体中的 code 才是可区分依据）
ERROR_STATUS_MAP: dict[str, int] = {
    "version_missing": 400,
    "version_unsupported": 400,
    "invalid_field": 422,
    "idempotency_key_missing": 400,
    "idempotency_conflict": 409,
    "concurrency_limit_exceeded": 429,
    "timeout": 504,
    "cancelled": 503,
    "external_dependency_failed": 502,
    "shutting_down": 503,
    "internal_error": 500,
}


async def _disconnect_watcher(request: Request, cancel_event: threading.Event) -> None:
    """客户端断连 -> 置位取消信号；结果仍然确定（取消或超时），无悬挂状态。"""
    try:
        while True:
            disconnected = await request.is_disconnected()
            if disconnected:
                cancel_event.set()
                return
            await asyncio.sleep(0.02)
    except asyncio.CancelledError:
        return


def create_app(service: OrderService | None = None) -> FastAPI:
    """构造 FastAPI 应用；lifespan 退出时执行优雅关停。"""
    svc = service or OrderService()
    configure_logging()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.service = svc
        yield
        # 关停：拒绝新请求并等待在途请求得出确定结论
        await asyncio.to_thread(svc.shutdown, 10.0)

    app = FastAPI(title="versioned-idempotent-api", lifespan=lifespan)
    app.state.service = svc

    @app.exception_handler(ServiceError)
    async def _on_service_error(request: Request, exc: ServiceError):
        from app.context import get_request_id
        rid = (exc.request_id or request.headers.get("x-request-id")
               or get_request_id())
        exc.request_id = rid
        status = ERROR_STATUS_MAP.get(exc.code.value, 500)
        return JSONResponse(
            exc.to_dict(),
            status_code=status,
            headers={"X-Request-ID": rid},
        )

    @app.get("/")
    def root():
        return {"service": "order-service", "status": "ok"}

    @app.get("/health")
    def health():
        return {
            "status": "shutting_down" if svc.store.shutting_down else "ok",
            "in_flight": svc.store.in_flight(),
        }

    @app.post("/orders")
    async def create_order(request: Request):
        request_id = request.headers.get("x-request-id") or f"http-{id(request):x}"
        set_request_id(request_id)

        try:
            payload = await request.json()
        except Exception:
            raise InvalidFieldError(
                "request body must be a valid JSON object", request_id=request_id
            )
        if not isinstance(payload, dict):
            raise InvalidFieldError(
                "request body must be a JSON object", request_id=request_id
            )

        cancel_event = threading.Event()
        watcher = asyncio.create_task(
            _disconnect_watcher(request, cancel_event), name="disconnect-watch"
        )
        try:
            # 业务逻辑在工作线程执行，事件循环保持可响应断连检测
            result, replayed = await asyncio.to_thread(
                svc.create_order,
                version_raw=request.headers.get("x-api-version"),
                idempotency_key=request.headers.get("idempotency-key"),
                payload=payload,
                request_id=request_id,
                cancel_event=cancel_event,
            )
        finally:
            watcher.cancel()

        body = build_response(result, result.api_version)
        headers = {
            "X-Request-ID": request_id,
            "Idempotent-Replay": "true" if replayed else "false",
        }
        return JSONResponse(body, status_code=201 if not replayed else 200,
                            headers=headers)

    return app
