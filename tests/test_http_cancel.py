"""真实 HTTP 服务器下，客户端在处理途中断开 -> 服务端取消且无中间状态。

TestClient 不模拟断连，这里用 uvicorn 起一个真实端口，
用裸 socket 发请求后立即关闭，再验证服务端状态确定。
"""

from __future__ import annotations

import socket
import time

import uvicorn

from app.http import create_app
from app.service import OrderService


def test_client_disconnect_cancels_request() -> None:
    svc = OrderService(max_in_flight=4, request_timeout=5.0)
    svc.warehouse.delay = 2.0  # 明显慢于断连时刻

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    host, port = listener.getsockname()

    config = uvicorn.Config(create_app(svc), fd=listener.fileno(),
                            log_level="error")
    server = uvicorn.Server(config)

    import threading
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not getattr(server, "started", False):
        time.sleep(0.01)

    try:
        sock = socket.create_connection((host, port), timeout=2.0)
        request = (
            "POST /orders HTTP/1.1\r\nHost: x\r\n"
            "X-Api-Version: v1\r\nIdempotency-Key: disconnect-k\r\n"
            "Content-Type: application/json\r\nContent-Length: 23\r\n"
            "Connection: close\r\n\r\n"
            '{"item":"a","quantity":1}'
        )
        sock.sendall(request.encode())
        time.sleep(0.2)
        sock.close()  # 处理途中客户端断开

        # 等待并确认：取消路径生效，外部与订单均无写入；名额最终释放
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and svc.store.in_flight() != 0:
            time.sleep(0.02)
        assert svc.store.in_flight() == 0
        assert svc.order_count() == 0
        assert svc.warehouse.count() == 0
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)
        listener.close()
