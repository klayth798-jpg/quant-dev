from contextlib import asynccontextmanager
from typing import Any, Dict

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from quantdev.config import PROJECT_ROOT, settings
from quantdev.db import database
from quantdev.models import (
    AgentResearchRequest,
    BacktestRequest,
    FactorEvaluateRequest,
    PaperOrderRequest,
    RiskCheckRequest,
    TushareSyncRequest,
)
from quantdev.services.agent import research_agent_service
from quantdev.services.backtest import backtest_service
from quantdev.services.execution import paper_execution_service
from quantdev.services.factors import factor_service
from quantdev.services.market import market_data_service
from quantdev.services.risk import risk_service
from quantdev.services.tushare_sync import tushare_sync_service
from quantdev.store import store


def _ensure_demo_results() -> None:
    factors = store.list_factors()
    for factor in factors:
        if not factor["latest_metrics"]:
            factor_service.evaluate(factor["factor_id"], forward_days=5)
    if not store.list_backtests(limit=1):
        backtest_service.run(BacktestRequest())


@asynccontextmanager
async def lifespan(_: FastAPI):
    database.migrate()
    market_data_service.ensure_factor_definitions()
    if settings.data_mode == "demo":
        market_data_service.bootstrap_demo_data()
        _ensure_demo_results()
    yield


app = FastAPI(
    title="Quant Dev API",
    version="0.1.0",
    description="Quantitative research, factor evaluation, backtesting and risk API.",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8000", "http://localhost:8000"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "environment": settings.environment,
        "data_mode": settings.data_mode,
        "database": str(settings.database_path),
        "live_trading": "disabled",
        "agent_permissions": "read-only",
    }


@app.get("/api/dashboard")
def dashboard() -> Dict[str, Any]:
    factors = store.list_factors()
    backtests = store.list_backtests(limit=5)
    snapshot_id = store.latest_snapshot_id()
    price_summary = (
        store.price_summary(snapshot_id)
        if snapshot_id
        else {
            "row_count": 0,
            "instrument_count": 0,
            "start_date": None,
            "end_date": None,
        }
    )
    latest = backtests[0] if backtests else None
    return {
        "snapshot_id": snapshot_id,
        "instrument_count": price_summary["instrument_count"],
        "price_row_count": price_summary["row_count"],
        "date_range": (
            [price_summary["start_date"], price_summary["end_date"]]
            if price_summary["start_date"]
            else []
        ),
        "factor_count": len(factors),
        "approved_factor_count": sum(
            1 for factor in factors if factor["status"] == "approved"
        ),
        "latest_backtest": latest,
        "recent_backtests": backtests,
        "system_status": [
            {"name": "研究数据库", "status": "healthy"},
            {"name": "因子计算引擎", "status": "healthy"},
            {"name": "回测引擎", "status": "healthy"},
            {"name": "实盘交易", "status": "disabled"},
        ],
    }


@app.post("/api/demo/bootstrap")
def bootstrap_demo(force: bool = False) -> Dict[str, Any]:
    result = market_data_service.bootstrap_demo_data(force=force)
    _ensure_demo_results()
    return result


@app.get("/api/instruments")
def instruments() -> Dict[str, Any]:
    return {"items": store.list_instruments()}


@app.get("/api/data/status")
def data_status() -> Dict[str, Any]:
    return tushare_sync_service.status()


@app.get("/api/data/sync-runs")
def data_sync_runs(
    limit: int = Query(default=20, ge=1, le=100),
) -> Dict[str, Any]:
    return {"items": store.list_data_sync_runs(limit=limit)}


@app.get("/api/data/sync-runs/{run_id}")
def data_sync_run(run_id: str) -> Dict[str, Any]:
    try:
        return tushare_sync_service.get_run(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/data/tushare/sync", status_code=202)
def sync_tushare(
    request: TushareSyncRequest,
    background_tasks: BackgroundTasks,
) -> Dict[str, Any]:
    if not settings.tushare_token:
        token_name = (
            "TINYSHARE_TOKEN"
            if settings.market_data_sdk == "tinyshare"
            else "TUSHARE_TOKEN"
        )
        raise HTTPException(
            status_code=409,
            detail=f"请在项目根目录 .env 配置 {token_name} 并重启服务",
        )
    try:
        run = tushare_sync_service.create_run(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    background_tasks.add_task(tushare_sync_service.run, run["run_id"], request)
    return run


@app.get("/api/data/indices")
def data_indices() -> Dict[str, Any]:
    return {"items": store.list_indices()}


@app.get("/api/data/financials/{symbol}")
def data_financials(
    symbol: str,
    limit: int = Query(default=20, ge=1, le=100),
) -> Dict[str, Any]:
    return {"symbol": symbol, "items": store.list_financial_indicators(symbol, limit)}


@app.get("/api/market/prices")
def market_prices(
    symbol: str = Query(..., min_length=3),
    limit: int = Query(default=260, ge=20, le=1000),
) -> Dict[str, Any]:
    rows = store.list_prices(symbol=symbol, snapshot_id=store.latest_snapshot_id())
    if not rows:
        raise HTTPException(status_code=404, detail="未找到标的行情")
    return {"symbol": symbol, "items": rows[-limit:]}


@app.get("/api/factors")
def factors() -> Dict[str, Any]:
    return {"items": store.list_factors(), "snapshot_id": store.latest_snapshot_id()}


@app.post("/api/factors/{factor_id}/evaluate")
def evaluate_factor(
    factor_id: str, request: FactorEvaluateRequest
) -> Dict[str, Any]:
    try:
        return factor_service.evaluate(
            factor_id, request.forward_days, request.neutralize
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/backtests")
def backtests(limit: int = Query(default=20, ge=1, le=100)) -> Dict[str, Any]:
    return {"items": store.list_backtests(limit=limit)}


@app.post("/api/backtests")
def run_backtest(request: BacktestRequest) -> Dict[str, Any]:
    try:
        return backtest_service.run(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/backtests/{run_id}")
def backtest_detail(run_id: str) -> Dict[str, Any]:
    result = store.get_backtest(run_id)
    if not result:
        raise HTTPException(status_code=404, detail="回测不存在")
    return result


@app.post("/api/risk/check")
def risk_check(request: RiskCheckRequest) -> Dict[str, Any]:
    return risk_service.check(request)


@app.get("/api/paper/account")
def paper_account() -> Dict[str, Any]:
    return paper_execution_service.get_account()


@app.post("/api/paper/orders")
def submit_paper_order(request: PaperOrderRequest) -> Dict[str, Any]:
    try:
        return paper_execution_service.submit(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/agent/research")
def agent_research(request: AgentResearchRequest) -> Dict[str, Any]:
    return research_agent_service.research(request)


@app.get("/api/system/requirements")
def system_requirements() -> Dict[str, Any]:
    market_provider = (
        "Tinyshare 代理接口"
        if settings.market_data_sdk == "tinyshare"
        else "Tushare Pro"
    )
    market_token_env = (
        "TINYSHARE_TOKEN"
        if settings.market_data_sdk == "tinyshare"
        else "TUSHARE_TOKEN"
    )
    return {
        "required_now": [],
        "optional_data_apis": [
            {
                "name": market_provider,
                "env": market_token_env,
                "purpose": "A股日线、财务数据、交易日历和指数成分",
                "status": "configured" if settings.tushare_token else "not_configured",
            },
            {
                "name": "deep-research-quant",
                "env": "DEEP_RESEARCH_BASE_URL / DEEP_RESEARCH_API_KEY",
                "purpose": "复用已有公告、新闻和深度研究 Agent",
                "status": "configured"
                if settings.deep_research_base_url
                else "not_configured",
            },
        ],
        "future_broker_access": [
            {
                "name": "券商量化交易接口",
                "purpose": "模拟盘稳定后接入报单、撤单、成交回报和账户查询",
                "status": "disabled_by_design",
            }
        ],
        "security_note": "不要把任何 API Key 提交到 Git；仅写入本地 .env 或部署密钥系统。",
    }


FRONTEND_DIR = PROJECT_ROOT / "frontend"
ASSET_DIR = FRONTEND_DIR / "assets"
app.mount("/assets", StaticFiles(directory=str(ASSET_DIR)), name="assets")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/{path:path}", include_in_schema=False)
def frontend_fallback(path: str) -> FileResponse:
    requested = (FRONTEND_DIR / path).resolve()
    if requested.is_file() and FRONTEND_DIR.resolve() in requested.parents:
        return FileResponse(requested)
    return FileResponse(FRONTEND_DIR / "index.html")
