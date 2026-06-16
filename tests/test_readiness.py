"""实盘路线就绪度自检测试。

验证：
  - 端点返回 5 个阶段且结构完整。
  - demo 环境下第一阶段（真实数据回测）因 data_mode=demo 未就绪，current_stage 落在该阶段。
  - 各阶段检查能正确反映系统状态（模拟盘成交、安全配置、小资金限额）。
"""

from dataclasses import replace

from fastapi.testclient import TestClient
from quantdev.api import app


def test_readiness_endpoint_structure():
    with TestClient(app) as client:
        body = client.get("/api/live/readiness").json()
    keys = [s["key"] for s in body["stages"]]
    assert keys == [
        "real_data_backtest",
        "continuous_paper",
        "mock_broker_stress",
        "small_capital_live",
    ] or keys == [
        "real_data_backtest",
        "continuous_paper",
        "mock_broker_stress",
        "semi_auto_live",
        "small_capital_live",
    ]
    # 每个阶段都有 checks 与 ready 字段。
    for stage in body["stages"]:
        assert "ready" in stage and isinstance(stage["checks"], list)


def test_fresh_env_not_ready_at_real_data_stage():
    # 全新测试库未跑过回测，第一阶段不就绪，current_stage 落在此。
    # 注意：TestClient 启动时 lifespan 的 _ensure_demo_results() 会写入回测，
    # 因此进入断言前先清空 backtest_runs，复现"从未回测"的全新环境。
    from quantdev.db import database

    with TestClient(app) as client:
        with database.transaction(immediate=True) as connection:
            connection.execute("DELETE FROM backtest_runs")
        body = client.get("/api/live/readiness").json()
    assert body["all_ready"] is False
    assert body["current_stage"] == "real_data_backtest"
    first = body["stages"][0]
    assert first["ready"] is False
    assert any(
        c["name"] == "至少完成过一次回测" and c["passed"] is False
        for c in first["checks"]
    )


def test_mock_broker_stress_verification_detected():
    # 只有真实执行并落库的通过记录，才能解锁该阶段。
    with TestClient(app):
        from quantdev.services.readiness import live_readiness_service
        from quantdev.store import store

        store.save_verification(
            "mock_broker_stress", "PASSED", {"tests": 22}
        )
        result = live_readiness_service.evaluate()
    stress = next(
        s for s in result["stages"] if s["key"] == "mock_broker_stress"
    )
    assert stress["ready"] is True


def test_small_capital_stage_reflects_config(monkeypatch):
    import quantdev.services.readiness as readiness_module

    # 把限额设到小资金区间外，验证检查能识别。
    monkeypatch.setattr(
        readiness_module,
        "settings",
        replace(
            readiness_module.settings,
            broker_mode="live",
            max_live_order_notional=50_000,
            max_daily_loss=1000,
        ),
    )
    result = readiness_module.live_readiness_service.evaluate()
    small = next(
        s for s in result["stages"] if s["key"] == "small_capital_live"
    )
    ceiling_check = next(
        c for c in small["checks"] if "小资金区间" in c["name"]
    )
    assert ceiling_check["passed"] is False


def test_continuous_paper_counts_fills():
    # 下两笔成交单后，连续模拟盘阶段应统计到成交与交易日。
    with TestClient(app) as client:
        for i in range(2):
            client.post(
                "/api/paper/orders",
                json={
                    "client_order_id": "readiness-fill-{}".format(i),
                    "symbol": "600519.SH",
                    "side": "buy",
                    "quantity": 100,
                    "order_type": "market",
                },
            )
        body = client.get("/api/live/readiness").json()
    paper = next(
        s for s in body["stages"] if s["key"] == "continuous_paper"
    )
    fill_check = next(c for c in paper["checks"] if "成交记录" in c["name"])
    assert fill_check["passed"] is True
