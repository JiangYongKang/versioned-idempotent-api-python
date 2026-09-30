"""进程启动入口。

- ``python main.py`` 直接以 uvicorn 启动 HTTP 服务。
- 也可通过 ``uvicorn main:app`` 使用。
"""

from __future__ import annotations

import uvicorn

from app.http import create_app

app = create_app()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
