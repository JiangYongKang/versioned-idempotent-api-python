"""命令行入口：``python -m versioned_orders [--port N]``。"""

from __future__ import annotations

import argparse

import uvicorn

from .api import create_app
from .logging_setup import configure_logging
from .service import GovernanceConfig


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="versioned_orders",
        description="Versioned + idempotent demo orders service",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--max-inflight", type=int, default=8)
    parser.add_argument("--request-timeout", type=float, default=3.0)
    parser.add_argument("--shutdown-grace", type=float, default=5.0)
    args = parser.parse_args()

    configure_logging()
    config = GovernanceConfig(
        max_inflight=args.max_inflight,
        request_timeout=args.request_timeout,
        shutdown_grace=args.shutdown_grace,
    )
    uvicorn.run(create_app_with_config(config), host=args.host, port=args.port)


def create_app_with_config(config: GovernanceConfig):
    def _factory():
        return create_app(config=config)
    return _factory


if __name__ == "__main__":
    main()
