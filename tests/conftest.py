from dataclasses import replace
from datetime import datetime, timezone

import pytest
from quantdev.db import database
from quantdev.models import QuoteUpdateRequest
from quantdev.services.market import market_data_service
from quantdev.services.quotes import quote_service
from quantdev.store import store

# 引用 quantdev.config.settings 的所有模块。每个模块通过
# `from quantdev.config import settings` 持有独立的名字绑定，因此必须逐一替换，
# 否则本机 .env（如生产配置）会泄漏进测试，导致 admin 鉴权 / broker 模式等断言失败。
_SETTINGS_MODULES = (
    "quantdev.api",
    "quantdev.db",
    "quantdev.integrations.broker",
    "quantdev.integrations.market_data",
    "quantdev.services.agent",
    "quantdev.services.calendar",
    "quantdev.services.execution",
    "quantdev.services.execution_router",
    "quantdev.services.live_execution",
    "quantdev.services.live_guard",
    "quantdev.services.pretrade",
    "quantdev.services.quotes",
    "quantdev.services.readiness",
    "quantdev.services.reconciliation",
    "quantdev.services.strategy",
    "quantdev.services.tasks",
    "quantdev.services.tushare_sync",
)


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    import importlib

    from quantdev.config import settings as base_settings

    original_path = database.path
    database.path = tmp_path / "quantdev-test.db"
    database.migrate()
    market_data_service.bootstrap_demo_data(force=True)

    # 统一的测试基线：与各测试用例假设的 fresh/demo 环境一致，屏蔽 .env 干扰。
    test_settings = replace(
        base_settings,
        environment="development",
        data_mode="demo",
        task_backend="inline",
        broker_mode="disabled",
        live_trading_enabled=False,
        admin_api_key="",
        admin_allow_insecure=True,
        paper_enforce_session=False,
        paper_quote_mode="realtime",
        calendar_fail_closed=False,
    )
    for module_name in _SETTINGS_MODULES:
        module = importlib.import_module(module_name)
        if hasattr(module, "settings"):
            monkeypatch.setattr(module, "settings", test_settings)
    now = datetime.now(timezone.utc)
    for symbol, price in store.latest_prices().items():
        quote_service.ingest(
            QuoteUpdateRequest(
                symbol=symbol,
                price=price,
                bid=price,
                ask=price,
                quote_time=now,
                source="test-feed",
            )
        )
    yield
    database.path = original_path
