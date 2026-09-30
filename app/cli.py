"""命令行直调入口：不经过 HTTP，直接调用同一套核心服务。

用于演示与验证“不同入口结论一致”。示例：

    uv run python -m app.cli --version v2 --key k-1 --item book --quantity 2
    uv run python -m app.cli --version v2 --key k-1 --item book --quantity 2   # 重放
    uv run python -m app.cli --inject-delay 3 --timeout 1 --key slow ...        # 超时
    uv run python -m app.cli --force-fail --key boom ...                       # 外部失败
"""

from __future__ import annotations

import argparse
import json
import sys

from app.errors import ServiceError
from app.logging_setup import configure_logging
from app.service import OrderService
from app.versioning import ApiVersion


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Versioned idempotent order CLI")
    p.add_argument("--version", required=True, help="API 版本：v1 / v2")
    p.add_argument("--key", required=True, help="幂等键 Idempotency-Key")
    p.add_argument("--item", required=True)
    p.add_argument("--quantity", type=int, required=True)
    p.add_argument("--priority", default="normal", choices=["normal", "high"])
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--max-in-flight", type=int, default=4)
    p.add_argument("--inject-delay", type=float, default=0.0,
                   help="让模拟外部依赖延迟 N 秒（复现超时）")
    p.add_argument("--force-fail", action="store_true",
                   help="让模拟外部依赖强制失败")
    p.add_argument("--request-id", default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging()
    svc = OrderService(max_in_flight=args.max_in_flight,
                       request_timeout=args.timeout)
    svc.warehouse.delay = args.inject_delay
    svc.warehouse.force_fail = args.force_fail

    payload: dict = {"item": args.item, "quantity": args.quantity}
    if args.version == ApiVersion.V2.value:
        payload["priority"] = args.priority

    try:
        result, replayed = svc.create_order(
            version_raw=args.version,
            idempotency_key=args.key,
            payload=payload,
            request_id=args.request_id,
        )
    except ServiceError as exc:
        json.dump(exc.to_dict(), sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 2

    body = svc.to_response(result, ApiVersion(args.version))
    body["idempotent_replay"] = replayed
    json.dump(body, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
