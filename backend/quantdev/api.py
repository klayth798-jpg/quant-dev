import hmac
import logging
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any, Dict

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from quantdev.alerting import alert_service, install_event_alerts
from quantdev.config import PROJECT_ROOT, settings
from quantdev.db import database
from quantdev.integrations.broker import get_broker
from quantdev.models import (
    AgentResearchRequest,
    BacktestRequest,
    FactorEvaluateRequest,
    IntentApprovalRequest,
    IntentRejectRequest,
    KillSwitchRequest,
    PaperOrderRequest,
    QuoteUpdateRequest,
    RiskCheckRequest,
    StrategyConfigRequest,
    StrategyEnableRequest,
    StrategyRunRequest,
    TushareSyncRequest,
)
from quantdev.services.agent import research_agent_service
from quantdev.services.backtest import backtest_service
from quantdev.services.calendar import trading_calendar_service
from quantdev.services.execution import paper_execution_service
from quantdev.services.execution_router import execution_router
from quantdev.services.factors import factor_service
from quantdev.services.live_execution import live_execution_service
from quantdev.services.live_guard import live_guard
from quantdev.services.market import market_data_service
from quantdev.services.quotes import quote_service
from quantdev.services.readiness import live_readiness_service
from quantdev.services.reconciliation import reconciliation_service
from quantdev.services.risk import risk_service
from quantdev.services.strategy import strategy_runner_service
from quantdev.services.tasks import TaskQueueUnavailable, task_service
from quantdev.services.tradability import tradability_service
from quantdev.services.tushare_sync import tushare_sync_service
from quantdev.store import new_id, store


def require_admin(
    x_admin_key: str = Header(default=""),
    x_operator: str = Header(default="local-admin"),
) -> str:
    # 每操作员独立密钥（推荐，sec-1/MC-1）：身份由密钥反查派生，不信任客户端自报的
    # X-Operator 头，使双人复核可信。配置 QUANTDEV_OPERATOR_KEYS 后即取代单一共享 key。
    if settings.operator_keys:
        for key, operator in settings.operator_keys.items():
            if hmac.compare_digest(x_admin_key, key):
                return operator
        raise HTTPException(status_code=401, detail="管理员密钥无效")
    if not settings.admin_api_key:
        # 安全默认：未配置管理员密钥时，写操作一律关闭。仅当显式开启
        # QUANTDEV_ALLOW_INSECURE_ADMIN（本地开发/测试）才放行，避免“一字之差全开”。
        if not settings.admin_allow_insecure:
            raise HTTPException(
                status_code=503,
                detail="未配置 QUANTDEV_ADMIN_API_KEY，写操作已关闭",
            )
        return x_operator
    if not hmac.compare_digest(x_admin_key, settings.admin_api_key):
        raise HTTPException(status_code=401, detail="管理员密钥无效")
    return x_operator


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
    install_event_alerts()
    market_data_service.ensure_factor_definitions()
    if settings.data_mode == "demo":
        market_data_service.bootstrap_demo_data()
        _ensure_demo_results()
    paper_execution_service.recover_active_orders()
    # 实盘模式：启动时先收敛本地中间态订单到券商真值，避免重启后 UNKNOWN 永久悬挂。
    if execution_router.configured_mode() == "live":
        try:
            live_execution_service.recover_active_orders()
        except Exception:  # 恢复失败不阻塞启动，但要告警。
            logging.getLogger(__name__).exception("实盘订单启动恢复失败")
            alert_service.send(
                "实盘订单启动恢复失败", severity="critical", dedup_key="live_recover_fail"
            )
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
    broker_mode = live_guard.resolved_broker_mode()
    return {
        "status": "ok",
        "environment": settings.environment,
        "data_mode": settings.data_mode,
        "database": database.display_name,
        "database_backend": database.backend,
        "task_backend": settings.task_backend,
        "database_healthy": database.ping(),
        "queue_healthy": task_service.ping(),
        "live_trading": "enabled" if broker_mode == "live" else "disabled",
        "broker_mode": broker_mode,
        "kill_switch_active": live_guard.kill_switch_active(),
        "agent_permissions": "read-only",
    }


@app.get("/api/health/live")
def liveness() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/api/health/ready")
def readiness() -> Dict[str, Any]:
    database_healthy = database.ping()
    queue_healthy = task_service.ping()
    result = {
        "status": "ready" if database_healthy and queue_healthy else "not_ready",
        "database": database_healthy,
        "queue": queue_healthy,
    }
    if result["status"] != "ready":
        raise HTTPException(status_code=503, detail=result)
    return result


@app.get("/api/live/guard", dependencies=[Depends(require_admin)])
def live_guard_status() -> Dict[str, Any]:
    return {
        "broker_mode": live_guard.resolved_broker_mode(),
        "configured_broker_mode": settings.broker_mode,
        "live_trading_enabled": settings.live_trading_enabled,
        "require_order_approval": settings.require_order_approval,
        "max_live_order_notional": settings.max_live_order_notional,
        "max_daily_loss": settings.max_daily_loss,
        "kill_switch": live_guard.kill_switch_status(),
    }


@app.post("/api/live/kill-switch", dependencies=[Depends(require_admin)])
def toggle_kill_switch(request: KillSwitchRequest) -> Dict[str, Any]:
    reason = request.reason or ("手动激活" if request.active else "手动解除")
    if request.active:
        record = live_guard.activate_kill_switch(reason)
    else:
        record = live_guard.deactivate_kill_switch(reason)
    return {"kill_switch": live_guard.kill_switch_status(), "record": record}


@app.get("/api/live/broker", dependencies=[Depends(require_admin)])
def live_broker_snapshot() -> Dict[str, Any]:
    """当前生效 Broker 的健康与账户/持仓/委托/成交查询视图。

    实盘里查询能力比下单更重要：这里把 Broker 的查询接口聚合成一份只读快照。
    """
    broker = get_broker()
    health = broker.health_check()
    account = broker.get_account()
    return {
        "health": asdict(health),
        "account": asdict(account),
        "positions": [asdict(item) for item in broker.get_positions()],
        "orders": [asdict(item) for item in broker.get_orders()],
        "trades": [asdict(item) for item in broker.get_trades()],
    }


@app.post("/api/live/reconcile", dependencies=[Depends(require_admin)])
def run_reconciliation() -> Dict[str, Any]:
    """触发一次日终对账：对比本地账本与 Broker 端持仓/现金快照。"""
    return reconciliation_service.run()


@app.post("/api/live/sync")
def sync_live_broker(_: str = Depends(require_admin)) -> Dict[str, Any]:
    try:
        return live_execution_service.sync_broker()
    except (PermissionError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/live/orders", dependencies=[Depends(require_admin)])
def list_live_orders() -> Dict[str, Any]:
    return {"items": store.list_live_orders()}


@app.post("/api/live/orders/{order_id}/cancel")
def cancel_live_order(
    order_id: str, _: str = Depends(require_admin)
) -> Dict[str, Any]:
    try:
        return live_execution_service.cancel_order(order_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/live/reconciliations", dependencies=[Depends(require_admin)])
def list_reconciliations(limit: int = Query(default=20, ge=1, le=100)) -> Dict[str, Any]:
    return {"items": store.list_reconciliations(limit=limit)}


@app.get("/api/live/reconciliations/{recon_id}", dependencies=[Depends(require_admin)])
def get_reconciliation(recon_id: str) -> Dict[str, Any]:
    report = store.get_reconciliation(recon_id)
    if report is None:
        raise HTTPException(status_code=404, detail="未找到该对账记录")
    return report


@app.get("/api/live/market-clock")
def market_clock() -> Dict[str, Any]:
    """A股交易时钟：当前是否交易日、是否交易时段、是否允许下单。"""
    return asdict(trading_calendar_service.market_clock())


@app.get("/api/live/tradability/{symbol}")
def tradability(symbol: str) -> Dict[str, Any]:
    """标的可交易性：是否停牌、当日涨跌停价与幅度。"""
    return asdict(tradability_service.evaluate(symbol))


@app.get("/api/events", dependencies=[Depends(require_admin)])
def list_events(
    limit: int = Query(default=50, ge=1, le=200),
    event_type: str = Query(default=None),
    aggregate_type: str = Query(default=None),
    aggregate_id: str = Query(default=None),
) -> Dict[str, Any]:
    """全局领域事件日志：按类型 / 聚合维度倒序查询，用于审计与回放。"""
    return {
        "items": store.list_domain_events(
            limit=limit,
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
        )
    }


@app.get("/api/live/readiness")
def live_readiness() -> Dict[str, Any]:
    """实盘路线 5 阶段就绪度自检：逐项校验每个阶段的进入条件。"""
    return live_readiness_service.evaluate()


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


@app.post("/api/demo/bootstrap", dependencies=[Depends(require_admin)])
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


@app.post(
    "/api/data/tushare/sync",
    status_code=202,
    dependencies=[Depends(require_admin)],
)
def sync_tushare(
    request: TushareSyncRequest,
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
    try:
        job = task_service.submit(
            "market_sync",
            {
                "run_id": run["run_id"],
                "request": request.model_dump(mode="json"),
            },
        )
    except TaskQueueUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {**run, "task_id": job["task_id"], "task_status": job["status"]}


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


@app.post(
    "/api/factors/{factor_id}/evaluate",
    status_code=202,
    dependencies=[Depends(require_admin)],
)
def evaluate_factor(
    factor_id: str, request: FactorEvaluateRequest
) -> Dict[str, Any]:
    try:
        return task_service.submit(
            "factor_evaluate",
            {
                "factor_id": factor_id,
                "forward_days": request.forward_days,
                "neutralize": request.neutralize,
            },
        )
    except TaskQueueUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/backtests")
def backtests(limit: int = Query(default=20, ge=1, le=100)) -> Dict[str, Any]:
    return {"items": store.list_backtests(limit=limit)}


@app.post(
    "/api/backtests",
    status_code=202,
    dependencies=[Depends(require_admin)],
)
def run_backtest(request: BacktestRequest) -> Dict[str, Any]:
    try:
        return task_service.submit(
            "backtest",
            {
                "run_id": new_id("bt"),
                "request": request.model_dump(mode="json"),
            },
        )
    except TaskQueueUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/backtests/{run_id}")
def backtest_detail(run_id: str) -> Dict[str, Any]:
    result = store.get_backtest(run_id)
    if not result:
        raise HTTPException(status_code=404, detail="回测不存在")
    return result


@app.get("/api/tasks")
def tasks(
    limit: int = Query(default=50, ge=1, le=200),
    status: str = Query(default=None),
) -> Dict[str, Any]:
    return {"items": store.list_task_jobs(limit=limit, status=status)}


@app.get("/api/tasks/{task_id}")
def task_detail(task_id: str) -> Dict[str, Any]:
    task = store.get_task_job(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


@app.post("/api/risk/check", dependencies=[Depends(require_admin)])
def risk_check(request: RiskCheckRequest) -> Dict[str, Any]:
    return risk_service.check(request)


@app.get("/api/paper/account", dependencies=[Depends(require_admin)])
def paper_account() -> Dict[str, Any]:
    return paper_execution_service.get_account()


@app.post("/api/paper/orders", dependencies=[Depends(require_admin)])
def submit_paper_order(request: PaperOrderRequest) -> Dict[str, Any]:
    try:
        return paper_execution_service.submit(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/api/paper/orders/{order_id}/cancel",
    dependencies=[Depends(require_admin)],
)
def cancel_paper_order(order_id: str) -> Dict[str, Any]:
    try:
        return paper_execution_service.cancel(order_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/paper/match", dependencies=[Depends(require_admin)])
def match_paper_orders(symbol: str = Query(default=None)) -> Dict[str, Any]:
    return paper_execution_service.match_open_orders(symbol)


@app.get("/api/paper/orders/{order_id}/events")
def paper_order_events(order_id: str) -> Dict[str, Any]:
    events = store.list_order_events(order_id)
    if not events:
        raise HTTPException(status_code=404, detail="未找到该订单的事件流")
    return {"order_id": order_id, "events": events}


@app.get("/api/live/quotes")
def list_quotes() -> Dict[str, Any]:
    return {"items": store.list_market_quotes()}


@app.post("/api/live/quotes", dependencies=[Depends(require_admin)])
def update_quote(request: QuoteUpdateRequest) -> Dict[str, Any]:
    try:
        return quote_service.ingest(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/strategies")
def list_strategies() -> Dict[str, Any]:
    return {"items": strategy_runner_service.list()}


@app.post("/api/strategies", dependencies=[Depends(require_admin)])
def create_strategy(request: StrategyConfigRequest) -> Dict[str, Any]:
    try:
        return strategy_runner_service.create(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post(
    "/api/strategies/{strategy_id}/enabled",
    dependencies=[Depends(require_admin)],
)
def set_strategy_enabled(
    strategy_id: str, request: StrategyEnableRequest
) -> Dict[str, Any]:
    try:
        return strategy_runner_service.set_enabled(strategy_id, request.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/strategies/run", dependencies=[Depends(require_admin)])
def run_strategy(request: StrategyRunRequest) -> Dict[str, Any]:
    try:
        return strategy_runner_service.run_once(request.strategy_id, request.force)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/strategies/runs")
def list_strategy_runs(
    limit: int = Query(default=50, ge=1, le=200),
) -> Dict[str, Any]:
    return {"items": strategy_runner_service.list_runs(limit)}


@app.get("/api/strategies/runs/{run_id}")
def strategy_run_detail(run_id: str) -> Dict[str, Any]:
    try:
        return strategy_runner_service.get_run(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/strategies/intents/{intent_id}/approve")
def approve_strategy_intent(
    intent_id: str,
    request: IntentApprovalRequest,
    actor: str = Depends(require_admin),
) -> Dict[str, Any]:
    try:
        return live_execution_service.approve_intent(
            intent_id, actor=actor, reason=request.reason
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/strategies/intents/{intent_id}/reject")
def reject_strategy_intent(
    intent_id: str,
    request: IntentRejectRequest,
    actor: str = Depends(require_admin),
) -> Dict[str, Any]:
    try:
        return live_execution_service.reject_intent(
            intent_id, request.reason, actor=actor
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/strategies/intents/{intent_id}/submit")
def submit_strategy_intent(
    intent_id: str, actor: str = Depends(require_admin)
) -> Dict[str, Any]:
    try:
        return live_execution_service.submit_intent(intent_id, actor=actor)
    except (PermissionError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/agent/research", dependencies=[Depends(require_admin)])
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
