import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from quantdev.alerting import alert_service
from quantdev.config import settings
from quantdev.db import database
from quantdev.models import StrategyConfigRequest
from quantdev.services.calendar import trading_calendar_service
from quantdev.services.execution_router import execution_router
from quantdev.services.factors import factor_service
from quantdev.services.live_execution import live_execution_service
from quantdev.services.quotes import quote_service
from quantdev.store import new_id, store, utc_now

LOGGER = logging.getLogger(__name__)
INTENT_ACTIVE_STATUSES = {
    "PENDING",
    "PENDING_APPROVAL",
    "APPROVED",
    "SUBMITTING",
    "SUBMITTED",
    "OPEN",
    "PARTIALLY_FILLED",
    "UNKNOWN",
    "CANCEL_PENDING",
    "BLOCKED",
}
INTENT_TERMINAL_STATUSES = {
    "FILLED",
    "CANCELLED",
    "PARTIALLY_CANCELLED",
    "REJECTED",
    "DRY_RUN",
}


class StrategyRunnerService:
    lease_key = "strategy-runner"
    _last_reconcile: Optional[float] = None  # 上次定时对账的 monotonic 时间

    def create(self, request: StrategyConfigRequest) -> Dict[str, Any]:
        if not store.get_factor(request.factor_id):
            raise ValueError("因子不存在: {}".format(request.factor_id))
        if request.order_type not in {"market", "limit"}:
            raise ValueError("策略订单类型只能是 market 或 limit")
        strategy_id = new_id("strat")
        now = utc_now()
        with database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO strategy_configs
                    (strategy_id, name, factor_id, top_n, rebalance_days,
                     universe_json, neutralize, order_type, max_order_notional,
                     enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    strategy_id,
                    request.name,
                    request.factor_id,
                    request.top_n,
                    request.rebalance_days,
                    json.dumps(request.universe_indices or [], ensure_ascii=True),
                    int(request.neutralize),
                    request.order_type,
                    request.max_order_notional,
                    int(request.enabled),
                    now,
                    now,
                ),
            )
        return self.get(strategy_id)

    def list(self) -> List[Dict[str, Any]]:
        with database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM strategy_configs ORDER BY created_at DESC"
            ).fetchall()
        return [self._decode_config(row) for row in rows]

    def get(self, strategy_id: str) -> Dict[str, Any]:
        with database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM strategy_configs WHERE strategy_id = ?",
                (strategy_id,),
            ).fetchone()
        if not row:
            raise ValueError("策略不存在")
        return self._decode_config(row)

    def set_enabled(self, strategy_id: str, enabled: bool) -> Dict[str, Any]:
        with database.transaction(immediate=True) as connection:
            updated = connection.execute(
                """
                UPDATE strategy_configs
                SET enabled = ?, updated_at = ?
                WHERE strategy_id = ?
                """,
                (int(enabled), utc_now(), strategy_id),
            ).rowcount
        if not updated:
            raise ValueError("策略不存在")
        return self.get(strategy_id)

    def run_once(self, strategy_id: str, force: bool = False) -> Dict[str, Any]:
        strategy = self.get(strategy_id)
        clock = trading_calendar_service.market_clock()
        if not clock.can_trade:
            raise ValueError("当前非交易时段，策略运行器不会生成订单")
        trade_date = trading_calendar_service.today().isoformat()
        snapshot_id = store.latest_snapshot_id()
        if not snapshot_id:
            raise ValueError("没有可用的数据快照")
        signal_day = trading_calendar_service.latest_completed_trading_day()
        if signal_day is None:
            raise ValueError("交易日历未覆盖当前日期")
        signal_date = signal_day.isoformat()
        latest_data_date = store.snapshot_max_date(snapshot_id)
        if latest_data_date != signal_date:
            raise ValueError(
                "行情快照已过期：最新 {}，策略需要 {}".format(
                    latest_data_date, signal_date
                )
            )
        existing = self._run_for_date(strategy_id, trade_date)
        if existing:
            existing = self.refresh_run(existing["run_id"])
            if force and existing["status"] in {"FAILED", "PARTIAL", "ACTIVE"}:
                return {
                    "idempotent": False,
                    **self._resume_existing(
                        existing,
                        strategy,
                        snapshot_id,
                        signal_date,
                    ),
                }
            return {"idempotent": True, **existing}
        if not force and not self._rebalance_due(strategy, trade_date):
            return {
                "idempotent": True,
                "status": "SKIPPED",
                "reason": "尚未到再平衡周期",
                "strategy_id": strategy_id,
                "trade_date": trade_date,
            }

        run_id = new_id("srun")
        now = utc_now()
        manifest = store.latest_dataset_manifest(snapshot_id)
        with database.transaction(immediate=True) as connection:
            inserted = connection.execute(
                """
                INSERT INTO strategy_runs
                    (run_id, strategy_id, trade_date, signal_date, snapshot_id,
                     manifest_id, status, started_at)
                VALUES (?, ?, ?, ?, ?, ?, 'RUNNING', ?)
                ON CONFLICT(strategy_id, trade_date) DO NOTHING
                """,
                (
                    run_id,
                    strategy_id,
                    trade_date,
                    signal_date,
                    snapshot_id,
                    manifest["manifest_id"] if manifest else None,
                    now,
                ),
            ).rowcount
        if not inserted:
            concurrent = self._run_for_date(strategy_id, trade_date)
            return {"idempotent": True, **concurrent}
        try:
            result = self._execute(run_id, strategy, snapshot_id, signal_date)
        except Exception as exc:
            with database.transaction(immediate=True) as connection:
                connection.execute(
                    """
                    UPDATE strategy_runs
                    SET status = 'FAILED', error_message = ?, finished_at = ?
                    WHERE run_id = ?
                    """,
                    (str(exc), utc_now(), run_id),
                )
            raise
        return {"idempotent": False, **result}

    def _execute(
        self,
        run_id: str,
        strategy: Dict[str, Any],
        snapshot_id: str,
        signal_date: str,
    ) -> Dict[str, Any]:
        universe = (
            store.constituent_symbols_at(strategy["universe_indices"], signal_date)
            if strategy["universe_indices"]
            else None
        )
        section = factor_service.latest_cross_section(
            strategy["factor_id"],
            snapshot_id,
            signal_date,
            symbols=universe,
            neutralize=strategy["neutralize"],
        )
        ranked = sorted(
            section["scores"].items(), key=lambda item: item[1], reverse=True
        )[: strategy["top_n"]]
        if not ranked:
            raise ValueError("最新截面没有可交易因子分数")
        account = execution_router.account()
        allocation = min(
            account["equity"] / len(ranked), strategy["max_order_notional"]
        )
        targets = []
        for symbol, score in ranked:
            quote = quote_service.get(symbol)
            if not quote.fresh or not quote.price:
                raise ValueError(
                    "{} 行情不可用：{}".format(symbol, quote.reason or "无价格")
                )
            quantity = int(allocation / quote.price / 100) * 100
            if quantity > 0:
                targets.append(
                    {
                        "symbol": symbol,
                        "score": score,
                        "target_weight": allocation / account["equity"],
                        "target_quantity": quantity,
                    }
                )
        if not targets:
            raise ValueError("单票资金上限不足以买入一手股票")
        current = {
            item["symbol"]: int(item["quantity"]) for item in account["positions"]
        }
        target_map = {item["symbol"]: item["target_quantity"] for item in targets}
        intents = []
        for symbol in sorted(set(current) | set(target_map)):
            delta = target_map.get(symbol, 0) - current.get(symbol, 0)
            if delta:
                quote = quote_service.get(symbol)
                intents.append(
                    {
                        "symbol": symbol,
                        "side": "buy" if delta > 0 else "sell",
                        "quantity": abs(delta),
                        "limit_price": (
                            quote.price
                            if strategy["order_type"] == "limit"
                            else None
                        ),
                    }
                )
        intents.sort(key=lambda item: 0 if item["side"] == "sell" else 1)
        self._persist_plan(
            run_id,
            targets,
            intents,
            strategy["order_type"],
            execution_router.configured_mode(),
        )
        return self._dispatch_pending(run_id)

    def _dispatch_pending(self, run_id: str) -> Dict[str, Any]:
        run = self.get_run(run_id)
        outcomes = []
        for intent in run["intents"]:
            if intent["status"] not in {"PENDING", "ERROR", "BLOCKED"}:
                continue
            intent_id = intent["intent_id"]
            try:
                outcome = execution_router.dispatch_intent(intent_id)
                outcomes.append(outcome)
            except Exception as exc:
                store.update_order_intent(
                    intent_id,
                    status="BLOCKED",
                    error_message=str(exc),
                )
                outcomes.append(
                    {"intent_id": intent_id, "status": "BLOCKED", "error": str(exc)}
                )
        return self.refresh_run(run_id)

    def _persist_plan(
        self,
        run_id: str,
        targets: List[Dict[str, Any]],
        intents: List[Dict[str, Any]],
        order_type: str,
        execution_mode: str,
    ) -> None:
        now = utc_now()
        with database.transaction(immediate=True) as connection:
            for target in targets:
                connection.execute(
                    """
                    INSERT INTO strategy_targets
                        (run_id, symbol, score, target_weight, target_quantity)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        target["symbol"],
                        target["score"],
                        target["target_weight"],
                        target["target_quantity"],
                    ),
                )
            for intent in intents:
                intent_id = new_id("intent")
                client_order_id = "{}:{}:{}".format(
                    run_id, intent["symbol"], intent["side"]
                )
                intent.update(
                    {"intent_id": intent_id, "client_order_id": client_order_id}
                )
                connection.execute(
                    """
                    INSERT INTO order_intents
                        (intent_id, run_id, client_order_id, symbol, side,
                         quantity, order_type, limit_price, status,
                         execution_mode, approval_status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?)
                    """,
                    (
                        intent_id,
                        run_id,
                        client_order_id,
                        intent["symbol"],
                        intent["side"],
                        intent["quantity"],
                        order_type,
                        intent.get("limit_price"),
                        execution_mode,
                        "PENDING" if execution_mode == "live" else "NOT_REQUIRED",
                        now,
                        now,
                    ),
                )

    def refresh_run(self, run_id: str) -> Dict[str, Any]:
        run = self.get_run(run_id)
        statuses = [item["status"] for item in run["intents"]]
        if not statuses:
            run_status = "FAILED" if run["status"] == "FAILED" else "COMPLETED"
        elif all(status == "FILLED" for status in statuses):
            run_status = "COMPLETED"
        elif any(status in INTENT_ACTIVE_STATUSES for status in statuses):
            run_status = (
                "AWAITING_APPROVAL"
                if all(
                    status in {"PENDING_APPROVAL", "APPROVED"}
                    for status in statuses
                )
                else "ACTIVE"
            )
        elif all(status == "REJECTED" for status in statuses):
            run_status = "FAILED"
        else:
            run_status = "PARTIAL"
        terminal = not any(status in INTENT_ACTIVE_STATUSES for status in statuses)
        with database.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE strategy_runs
                SET status = ?, error_message = NULL,
                    finished_at = CASE WHEN ? THEN COALESCE(finished_at, ?)
                                       ELSE NULL END
                WHERE run_id = ?
                """,
                (run_status, int(terminal), utc_now(), run_id),
            )
        return self.get_run(run_id)

    def refresh_intent(
        self,
        intent_id: str,
        status: str,
        *,
        paper_order_id: Optional[str] = None,
        broker_order_id: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> Dict[str, Any]:
        intent = store.update_order_intent(
            intent_id,
            status=status,
            paper_order_id=paper_order_id,
            broker_order_id=broker_order_id,
            error_message=error_message,
            clear_error=error_message is None,
        )
        return self.refresh_run(intent["run_id"])

    def _resume_existing(
        self,
        run: Dict[str, Any],
        strategy: Dict[str, Any],
        snapshot_id: str,
        signal_date: str,
    ) -> Dict[str, Any]:
        with database.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE strategy_runs
                SET status = 'RUNNING', error_message = NULL,
                    finished_at = NULL
                WHERE run_id = ?
                """,
                (run["run_id"],),
            )
        if run["intents"]:
            return self._dispatch_pending(run["run_id"])
        return self._execute(run["run_id"], strategy, snapshot_id, signal_date)

    def _rebalance_due(self, strategy: Dict[str, Any], trade_date: str) -> bool:
        with database.connect() as connection:
            row = connection.execute(
                """
                SELECT trade_date
                FROM strategy_runs
                WHERE strategy_id = ? AND status IN ('COMPLETED', 'PARTIAL')
                ORDER BY trade_date DESC LIMIT 1
                """,
                (strategy["strategy_id"],),
            ).fetchone()
        if not row:
            return True
        count = store.trading_day_count(row["trade_date"], trade_date)
        return count >= strategy["rebalance_days"]

    def _run_for_date(
        self, strategy_id: str, trade_date: str
    ) -> Optional[Dict[str, Any]]:
        with database.connect() as connection:
            row = connection.execute(
                """
                SELECT run_id FROM strategy_runs
                WHERE strategy_id = ? AND trade_date = ?
                """,
                (strategy_id, trade_date),
            ).fetchone()
        return self.get_run(row["run_id"]) if row else None

    def get_run(self, run_id: str) -> Dict[str, Any]:
        with database.connect() as connection:
            run = connection.execute(
                "SELECT * FROM strategy_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            targets = connection.execute(
                "SELECT * FROM strategy_targets WHERE run_id = ? ORDER BY score DESC",
                (run_id,),
            ).fetchall()
            intents = connection.execute(
                "SELECT * FROM order_intents WHERE run_id = ? ORDER BY created_at",
                (run_id,),
            ).fetchall()
        if not run:
            raise ValueError("策略运行记录不存在")
        result = dict(run)
        result["targets"] = [dict(row) for row in targets]
        result["intents"] = [dict(row) for row in intents]
        return result

    def list_runs(self, limit: int = 50) -> List[Dict[str, Any]]:
        with database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM strategy_runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def run_enabled(self) -> List[Dict[str, Any]]:
        owner = "runner-{}".format(uuid.uuid4().hex[:8])
        if not store.acquire_lease(
            self.lease_key, owner, settings.runner_lease_seconds
        ):
            return []
        try:
            if execution_router.configured_mode() == "paper":
                from quantdev.services.execution import paper_execution_service

                paper_execution_service.recover_active_orders()
            elif execution_router.configured_mode() == "live":
                # 每轮：同步券商→收敛中间态订单→按间隔自动对账（oms-1/recon-2）。
                # 三步彼此隔离：任一步骤的瞬时异常都不应拖垮其它步骤与策略执行，且要告警，
                # 不能像旧实现那样一个查询异常静默中断整轮。
                for step in (
                    live_execution_service.sync_broker,
                    live_execution_service.recover_active_orders,
                    self._maybe_reconcile,
                ):
                    try:
                        step()
                    except Exception:
                        name = getattr(step, "__name__", str(step))
                        LOGGER.exception("实盘运行器步骤失败: %s", name)
                        alert_service.send(
                            "实盘运行器步骤失败",
                            "step={}".format(name),
                            severity="high",
                            dedup_key="runner_step_fail:{}".format(name),
                        )
            results = []
            for strategy in self.list():
                if not store.renew_lease(
                    self.lease_key, owner, settings.runner_lease_seconds
                ):
                    raise RuntimeError("策略运行器租约已丢失")
                if strategy["enabled"]:
                    try:
                        results.append(self.run_once(strategy["strategy_id"]))
                    except (PermissionError, ValueError) as exc:
                        results.append(
                            {
                                "strategy_id": strategy["strategy_id"],
                                "status": "SKIPPED",
                                "reason": str(exc),
                            }
                        )
            return results
        finally:
            store.release_lease(self.lease_key, owner)

    def _maybe_reconcile(self) -> None:
        """按 reconciliation_interval_seconds 固定间隔自动对账（recon-2）。

        实盘对账不能只靠人工手动触发：在 live runner 每轮 sync 后按间隔触发一次，
        BREAK 会经事件总线触发企业微信告警并按配置自动激活 Kill Switch。
        """
        now = time.monotonic()
        if (
            self._last_reconcile is not None
            and now - self._last_reconcile < settings.reconciliation_interval_seconds
        ):
            return
        self._last_reconcile = now
        try:
            from quantdev.services.reconciliation import reconciliation_service

            reconciliation_service.run()
        except Exception:
            LOGGER.exception("定时对账失败")

    def loop(self) -> None:
        failures = 0
        while True:
            try:
                self.run_enabled()
                failures = 0
                delay = settings.runner_poll_seconds
            except Exception as exc:
                failures += 1
                delay = min(
                    settings.runner_poll_seconds * (2 ** min(failures, 5)),
                    300,
                )
                LOGGER.exception("策略运行器循环失败")
                store.audit(
                    actor="strategy-runner",
                    action="runner_loop_error",
                    resource_type="system",
                    resource_id=self.lease_key,
                    payload={"error": str(exc), "retry_seconds": delay},
                )
                alert_service.send(
                    "策略运行器循环失败",
                    "连续失败 {} 次，{}s 后重试；错误：{}".format(failures, delay, exc),
                    severity="high",
                    dedup_key="runner_loop_fail",
                )
            time.sleep(delay)

    @staticmethod
    def _decode_config(row) -> Dict[str, Any]:
        item = dict(row)
        item["universe_indices"] = json.loads(item.pop("universe_json"))
        item["neutralize"] = bool(item["neutralize"])
        item["enabled"] = bool(item["enabled"])
        return item


strategy_runner_service = StrategyRunnerService()
