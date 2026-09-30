"""HTTP 入口与进程内入口结论一致：状态码 + 错误码可区分。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.http import create_app
from app.service import OrderService


def _client() -> tuple[TestClient, OrderService]:
    svc = OrderService(max_in_flight=2, request_timeout=0.6)
    return TestClient(create_app(svc)), svc


def _post(client: TestClient, headers: dict | None = None, json=None):
    return client.post("/orders", headers=headers or {}, json=json)


def test_http_version_errors_distinguishable() -> None:
    client, _ = _client()
    body = {"item": "book", "quantity": 1}

    r = _post(client, {"Idempotency-Key": "k"}, body)  # 缺版本
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "version_missing"

    r = _post(client, {"X-Api-Version": "v9", "Idempotency-Key": "k"}, body)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "version_unsupported"

    r = _post(client, {"X-Api-Version": "v1", "Idempotency-Key": "k"},
              {"item": "book", "quantity": 0})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_field"

    r = _post(client, {"X-Api-Version": "v1"}, body)  # 缺幂等键
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "idempotency_key_missing"

    # 统一错误结构 + 请求标识回显
    payload = r.json()["error"]
    assert set(payload) == {"code", "message", "retryable", "request_id"}
    assert r.headers["x-request-id"]


def test_http_version_shapes_and_defaults() -> None:
    client, _ = _client()
    r1 = _post(client, {"X-Api-Version": "v1", "Idempotency-Key": "v1k"},
               {"item": "book", "quantity": 2})
    assert r1.status_code == 201
    assert set(r1.json()) == {"order_id", "item", "quantity"}  # v1 结构不变
    assert r1.headers["idempotent-replay"] == "false"

    r2 = _post(client, {"X-Api-Version": "v2", "Idempotency-Key": "v2k"},
               {"item": "pen", "quantity": 1})
    body2 = r2.json()
    assert body2["priority"] == "normal"          # 新字段稳定默认
    assert body2["api_version"] == "v2"


def test_http_idempotent_replay() -> None:
    client, _ = _client()
    headers = {"X-Api-Version": "v1", "Idempotency-Key": "same"}
    body = {"item": "book", "quantity": 1}
    first = _post(client, headers, body)
    second = _post(client, headers, body)
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.headers["idempotent-replay"] == "true"
    assert first.json()["order_id"] == second.json()["order_id"]


def test_http_idempotency_conflict() -> None:
    client, _ = _client()
    _post(client, {"X-Api-Version": "v1", "Idempotency-Key": "ck"},
          {"item": "a", "quantity": 1})
    r = _post(client, {"X-Api-Version": "v1", "Idempotency-Key": "ck"},
              {"item": "DIFFERENT", "quantity": 1})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "idempotency_conflict"


def test_http_concurrency_limit() -> None:
    client, svc = _client()
    svc.warehouse.delay = 1.0
    # 占满 2 个名额（TestClient 用后台线程发请求）
    import threading

    def hold(key: str) -> None:
        _post(client, {"X-Api-Version": "v1", "Idempotency-Key": key},
              {"item": "a", "quantity": 1})

    threads = [threading.Thread(target=hold, args=(f"h{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    import time
    time.sleep(0.3)

    r = _post(client, {"X-Api-Version": "v1", "Idempotency-Key": "over"},
              {"item": "b", "quantity": 1})
    assert r.status_code == 429
    data = r.json()["error"]
    assert data["code"] == "concurrency_limit_exceeded"
    assert data["retryable"] is True
    assert "limit 2" in data["message"]
    for t in threads:
        t.join()


def test_http_timeout() -> None:
    client, svc = _client()
    svc.warehouse.delay = 5.0
    r = _post(client, {"X-Api-Version": "v1", "Idempotency-Key": "slow"},
              {"item": "a", "quantity": 1})
    assert r.status_code == 504
    assert r.json()["error"]["code"] == "timeout"
    assert svc.order_count() == 0 and svc.warehouse.count() == 0


def test_http_external_failure() -> None:
    client, svc = _client()
    svc.warehouse.force_fail = True
    r = _post(client, {"X-Api-Version": "v1", "Idempotency-Key": "boom"},
              {"item": "a", "quantity": 1})
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "external_dependency_failed"


def test_http_bad_json_is_invalid_field() -> None:
    client, _ = _client()
    r = client.post(
        "/orders",
        headers={"X-Api-Version": "v1", "Idempotency-Key": "k"},
        content=b"{not-json",
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_field"


def test_http_lifespan_graceful_shutdown() -> None:
    """应用关闭（lifespan 退出）时触发服务优雅关停，新请求被明确拒绝。"""
    svc = OrderService(max_in_flight=2, request_timeout=1.0)
    with TestClient(create_app(svc)) as client:
        assert client.get("/health").json()["status"] == "ok"
    # with 退出后 lifespan 已执行 shutdown
    assert svc.store.shutting_down is True
