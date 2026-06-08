from dataclasses import replace

import quantdev.api as api_module
from fastapi.testclient import TestClient
from quantdev.api import app


def test_dashboard_and_factor_endpoints():
    with TestClient(app) as client:
        dashboard = client.get("/api/dashboard")
        factors = client.get("/api/factors")

    assert dashboard.status_code == 200
    assert dashboard.json()["instrument_count"] == 8
    assert factors.status_code == 200
    assert len(factors.json()["items"]) == 4


def test_risk_endpoint():
    with TestClient(app) as client:
        response = client.post(
            "/api/risk/check",
            json={
                "positions": [{"symbol": "600519.SH", "weight": 0.2}],
                "proposed_turnover": 0.3,
                "current_drawdown": 0.05,
                "order_notional_weight": 0.1,
            },
        )

    assert response.status_code == 200
    assert response.json()["approved"] is True


def test_tushare_sync_requires_token(monkeypatch):
    monkeypatch.setattr(
        api_module,
        "settings",
        replace(api_module.settings, tushare_token=""),
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/data/tushare/sync",
            json={
                "start_date": "2026-06-01",
                "end_date": "2026-06-03",
            },
        )

    assert response.status_code == 409
    assert "TUSHARE_TOKEN" in response.json()["detail"]
