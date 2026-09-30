"""根入口：保留原启动方式，转发到包内 FastAPI 应用。"""

from __future__ import annotations

import uvicorn

from versioned_orders.api import create_app

app = create_app()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
