"""测试公共夹具。"""

from __future__ import annotations

import logging

import pytest

from app.logging_setup import _LOGGER_NAME, configure_logging
from app.service import OrderService


@pytest.fixture(autouse=True)
def _attach_caplog(caplog: pytest.LogCaptureFixture) -> None:
    """业务 logger 不向 root 传播，这里显式接入 pytest 的捕获处理器。"""
    logger = configure_logging()
    logger.addHandler(caplog.handler)
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
    yield
    logger.removeHandler(caplog.handler)


@pytest.fixture
def service() -> OrderService:
    return OrderService(max_in_flight=2, request_timeout=1.0)
