import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from quantdev.db import database


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return "{}_{}".format(prefix, uuid.uuid4().hex[:16])


def decode_json(value: str) -> Any:
    return json.loads(value)


class Store:
    def list_instruments(self) -> List[Dict[str, Any]]:
        with database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM instruments WHERE active = 1 ORDER BY symbol"
            ).fetchall()
        return [dict(row) for row in rows]

    def list_prices(
        self, symbol: Optional[str] = None, snapshot_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM prices WHERE 1 = 1"
        params: List[Any] = []
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        if snapshot_id:
            query += " AND snapshot_id = ?"
            params.append(snapshot_id)
        query += " ORDER BY trade_date, symbol"
        with database.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def latest_snapshot_id(self) -> Optional[str]:
        with database.connect() as connection:
            row = connection.execute(
                "SELECT snapshot_id, MAX(trade_date) FROM prices GROUP BY snapshot_id "
                "ORDER BY MAX(trade_date) DESC LIMIT 1"
            ).fetchone()
        return str(row["snapshot_id"]) if row else None

    def price_summary(self, snapshot_id: str) -> Dict[str, Any]:
        with database.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS row_count,
                       COUNT(DISTINCT symbol) AS instrument_count,
                       MIN(trade_date) AS start_date,
                       MAX(trade_date) AS end_date
                FROM prices
                WHERE snapshot_id = ?
                """,
                (snapshot_id,),
            ).fetchone()
        return dict(row)

    def snapshot_max_date(self, snapshot_id: str) -> Optional[str]:
        """快照内全市场最新交易日（用于判断单个标的是否停牌/缺当日数据）。"""
        with database.connect() as connection:
            row = connection.execute(
                "SELECT MAX(trade_date) AS max_date FROM prices WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
        return row["max_date"] if row and row["max_date"] else None

    def symbol_price_tail(
        self, symbol: str, snapshot_id: str, limit: int = 2
    ) -> List[Dict[str, Any]]:
        """某标的在快照中按交易日倒序的最近 N 条行情（默认最近两日）。"""
        with database.connect() as connection:
            rows = connection.execute(
                """
                SELECT trade_date, close
                FROM prices
                WHERE snapshot_id = ? AND symbol = ?
                ORDER BY trade_date DESC
                LIMIT ?
                """,
                (snapshot_id, symbol, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_data_sync_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM data_sync_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["parameters"] = decode_json(item.pop("parameters_json"))
        item["stats"] = decode_json(item.pop("stats_json"))
        return item

    def list_data_sync_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        with database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM data_sync_runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["parameters"] = decode_json(item.pop("parameters_json"))
            item["stats"] = decode_json(item.pop("stats_json"))
            result.append(item)
        return result

    def list_indices(self) -> List[Dict[str, Any]]:
        with database.connect() as connection:
            rows = connection.execute(
                """
                SELECT i.*,
                       (SELECT MAX(c.trade_date) FROM index_constituents c
                        WHERE c.index_code = i.index_code) AS latest_weight_date,
                       (SELECT COUNT(*) FROM index_constituents c
                        WHERE c.index_code = i.index_code
                          AND c.trade_date = (
                              SELECT MAX(c2.trade_date) FROM index_constituents c2
                              WHERE c2.index_code = i.index_code
                          )) AS latest_constituent_count
                FROM indices i
                ORDER BY i.index_code
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_constituent_symbols(self, index_codes: List[str]) -> List[str]:
        if not index_codes:
            return []
        placeholders = ",".join("?" for _ in index_codes)
        query = f"""
            SELECT DISTINCT c.symbol
            FROM index_constituents c
            WHERE c.index_code IN ({placeholders})
              AND c.trade_date = (
                  SELECT MAX(c2.trade_date)
                  FROM index_constituents c2
                  WHERE c2.index_code = c.index_code
              )
            ORDER BY c.symbol
        """
        with database.connect() as connection:
            rows = connection.execute(query, index_codes).fetchall()
        return [str(row["symbol"]) for row in rows]

    def list_index_constituents(
        self, index_codes: List[str]
    ) -> List[Dict[str, Any]]:
        if not index_codes:
            return []
        placeholders = ",".join("?" for _ in index_codes)
        with database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT DISTINCT index_code, symbol, trade_date
                FROM index_constituents
                WHERE index_code IN ({placeholders})
                ORDER BY index_code, trade_date, symbol
                """,
                index_codes,
            ).fetchall()
        return [dict(row) for row in rows]

    def industry_map(self) -> Dict[str, str]:
        """返回 symbol -> 行业 的映射，用于行业中性化。"""
        with database.connect() as connection:
            rows = connection.execute(
                "SELECT symbol, industry FROM instruments"
            ).fetchall()
        return {str(row["symbol"]): str(row["industry"]) for row in rows}

    def market_cap_panel(self) -> Dict[str, Dict[str, float]]:
        """返回 trade_date -> {symbol: total_mv} 的市值面板，用于市值中性化。"""
        panel: Dict[str, Dict[str, float]] = {}
        with database.connect() as connection:
            rows = connection.execute(
                "SELECT trade_date, symbol, total_mv FROM daily_indicators "
                "WHERE total_mv IS NOT NULL AND total_mv > 0"
            ).fetchall()
        for row in rows:
            panel.setdefault(str(row["trade_date"]), {})[str(row["symbol"])] = float(
                row["total_mv"]
            )
        return panel

    def list_financial_indicators(
        self, symbol: str, limit: int = 20
    ) -> List[Dict[str, Any]]:
        with database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM financial_indicators
                WHERE symbol = ?
                ORDER BY end_date DESC, ann_date DESC
                LIMIT ?
                """,
                (symbol, limit),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["raw"] = decode_json(item.pop("raw_json"))
            result.append(item)
        return result

    def list_factors(self) -> List[Dict[str, Any]]:
        with database.connect() as connection:
            rows = connection.execute(
                """
                SELECT f.*,
                       (SELECT metrics_json FROM factor_runs r
                        WHERE r.factor_id = f.factor_id
                        ORDER BY r.created_at DESC LIMIT 1) AS latest_metrics_json,
                       (SELECT run_id FROM factor_runs r
                        WHERE r.factor_id = f.factor_id
                        ORDER BY r.created_at DESC LIMIT 1) AS latest_run_id
                FROM factor_definitions f
                ORDER BY f.category, f.factor_id
                """
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            raw_metrics = item.pop("latest_metrics_json")
            item["latest_metrics"] = decode_json(raw_metrics) if raw_metrics else None
            result.append(item)
        return result

    def get_factor(self, factor_id: str) -> Optional[Dict[str, Any]]:
        with database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM factor_definitions WHERE factor_id = ?", (factor_id,)
            ).fetchone()
        return dict(row) if row else None

    def save_factor_run(
        self,
        factor_id: str,
        snapshot_id: str,
        parameters: Dict[str, Any],
        metrics: Dict[str, Any],
    ) -> Dict[str, Any]:
        run_id = new_id("fac")
        created_at = utc_now()
        with database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO factor_runs
                    (run_id, factor_id, snapshot_id, parameters_json, metrics_json,
                     status, created_at)
                VALUES (?, ?, ?, ?, ?, 'completed', ?)
                """,
                (
                    run_id,
                    factor_id,
                    snapshot_id,
                    json.dumps(parameters, ensure_ascii=True),
                    json.dumps(metrics, ensure_ascii=True),
                    created_at,
                ),
            )
        return {
            "run_id": run_id,
            "factor_id": factor_id,
            "snapshot_id": snapshot_id,
            "parameters": parameters,
            "metrics": metrics,
            "status": "completed",
            "created_at": created_at,
        }

    def save_backtest(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO backtest_runs
                    (run_id, name, factor_id, snapshot_id, config_json, metrics_json,
                     equity_json, trades_json, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["run_id"],
                    payload["name"],
                    payload["factor_id"],
                    payload["snapshot_id"],
                    json.dumps(payload["config"], ensure_ascii=True),
                    json.dumps(payload["metrics"], ensure_ascii=True),
                    json.dumps(payload["equity_curve"], ensure_ascii=True),
                    json.dumps(payload["trades"], ensure_ascii=True),
                    payload["status"],
                    payload["created_at"],
                ),
            )
        return payload

    def list_backtests(self, limit: int = 20) -> List[Dict[str, Any]]:
        with database.connect() as connection:
            rows = connection.execute(
                """
                SELECT run_id, name, factor_id, snapshot_id, config_json, metrics_json,
                       status, created_at
                FROM backtest_runs
                ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["config"] = decode_json(item.pop("config_json"))
            item["metrics"] = decode_json(item.pop("metrics_json"))
            result.append(item)
        return result

    def get_backtest(self, run_id: str) -> Optional[Dict[str, Any]]:
        with database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM backtest_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["config"] = decode_json(item.pop("config_json"))
        item["metrics"] = decode_json(item.pop("metrics_json"))
        item["equity_curve"] = decode_json(item.pop("equity_json"))
        item["trades"] = decode_json(item.pop("trades_json"))
        return item

    def save_risk_event(
        self, severity: str, rule_code: str, message: str, payload: Dict[str, Any]
    ) -> None:
        with database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO risk_events
                    (event_id, severity, rule_code, message, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id("risk"),
                    severity,
                    rule_code,
                    message,
                    json.dumps(payload, ensure_ascii=True),
                    utc_now(),
                ),
            )

    def audit(
        self,
        actor: str,
        action: str,
        resource_type: str,
        resource_id: str,
        payload: Dict[str, Any],
    ) -> None:
        with database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO audit_log
                    (audit_id, actor, action, resource_type, resource_id,
                     payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id("audit"),
                    actor,
                    action,
                    resource_type,
                    resource_id,
                    json.dumps(payload, ensure_ascii=True),
                    utc_now(),
                ),
            )


    def get_flag(self, flag: str) -> Optional[Dict[str, Any]]:
        with database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM system_flags WHERE flag = ?", (flag,)
            ).fetchone()
        return dict(row) if row else None

    def calendar_entry(
        self, cal_date: str, exchange: str = "SSE"
    ) -> Optional[Dict[str, Any]]:
        """查询交易日历中某一天的记录；无数据时返回 None（由调用方决定兜底）。"""
        with database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM trade_calendar WHERE exchange = ? AND cal_date = ?",
                (exchange, cal_date),
            ).fetchone()
        return dict(row) if row else None

    def append_order_event(
        self,
        connection,
        order_id: str,
        event_type: str,
        payload: Dict[str, Any],
    ) -> None:
        """向订单事件流追加一条事件（append-only）。

        seq 在同一 order_id 下单调递增，复用调用方的事务连接以保证原子性。
        """
        row = connection.execute(
            "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM order_events WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        next_seq = int(row["max_seq"]) + 1
        connection.execute(
            """
            INSERT INTO order_events
                (event_id, order_id, seq, event_type, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("oevt"),
                order_id,
                next_seq,
                event_type,
                json.dumps(payload, ensure_ascii=True),
                utc_now(),
            ),
        )

    def list_order_events(self, order_id: str) -> List[Dict[str, Any]]:
        with database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM order_events WHERE order_id = ? ORDER BY seq",
                (order_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = decode_json(item.pop("payload_json"))
            result.append(item)
        return result

    def set_flag(
        self, flag: str, value: str, reason: str, updated_by: str
    ) -> Dict[str, Any]:
        now = utc_now()
        with database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO system_flags (flag, value, reason, updated_by, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(flag) DO UPDATE SET
                    value = excluded.value,
                    reason = excluded.reason,
                    updated_by = excluded.updated_by,
                    updated_at = excluded.updated_at
                """,
                (flag, value, reason, updated_by, now),
            )
        return {
            "flag": flag,
            "value": value,
            "reason": reason,
            "updated_by": updated_by,
            "updated_at": now,
        }

    def save_reconciliation(self, report: Dict[str, Any]) -> str:
        recon_id = new_id("recon")
        with database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO reconciliations
                    (recon_id, broker_mode, status, cash_diff, position_break_count,
                     breaks_json, local_json, broker_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    recon_id,
                    report["broker_mode"],
                    report["status"],
                    report["cash_diff"],
                    len(report["breaks"]),
                    json.dumps(report["breaks"], ensure_ascii=True),
                    json.dumps(report["local"], ensure_ascii=True),
                    json.dumps(report["broker"], ensure_ascii=True),
                    utc_now(),
                ),
            )
        return recon_id

    def list_reconciliations(self, limit: int = 20) -> List[Dict[str, Any]]:
        with database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM reconciliations ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._decode_reconciliation(row) for row in rows]

    def get_reconciliation(self, recon_id: str) -> Optional[Dict[str, Any]]:
        with database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM reconciliations WHERE recon_id = ?", (recon_id,)
            ).fetchone()
        return self._decode_reconciliation(row) if row else None

    @staticmethod
    def _decode_reconciliation(row) -> Dict[str, Any]:
        item = dict(row)
        item["breaks"] = decode_json(item.pop("breaks_json"))
        item["local"] = decode_json(item.pop("local_json"))
        item["broker"] = decode_json(item.pop("broker_json"))
        return item

    def record_domain_event(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: Dict[str, Any],
        actor: str = "system",
    ) -> Dict[str, Any]:
        """持久化一条全局领域事件（append-only），seq 全局单调递增。"""
        event_id = new_id("evt")
        created_at = utc_now()
        with database.transaction() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM domain_events"
            ).fetchone()
            next_seq = int(row["max_seq"]) + 1
            connection.execute(
                """
                INSERT INTO domain_events
                    (event_id, seq, event_type, aggregate_type, aggregate_id,
                     actor, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    next_seq,
                    event_type,
                    aggregate_type,
                    aggregate_id,
                    actor,
                    json.dumps(payload, ensure_ascii=True),
                    created_at,
                ),
            )
        return {
            "event_id": event_id,
            "seq": next_seq,
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "actor": actor,
            "payload": payload,
            "created_at": created_at,
        }

    def list_domain_events(
        self,
        limit: int = 50,
        event_type: Optional[str] = None,
        aggregate_type: Optional[str] = None,
        aggregate_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM domain_events WHERE 1 = 1"
        params: List[Any] = []
        if event_type:
            query += " AND event_type = ?"
            params.append(event_type)
        if aggregate_type:
            query += " AND aggregate_type = ?"
            params.append(aggregate_type)
        if aggregate_id:
            query += " AND aggregate_id = ?"
            params.append(aggregate_id)
        query += " ORDER BY seq DESC LIMIT ?"
        params.append(limit)
        with database.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = decode_json(item.pop("payload_json"))
            result.append(item)
        return result

    def paper_trading_stats(self) -> Dict[str, Any]:
        """模拟盘活跃度统计：成交订单数 + 覆盖的交易日数（按成交日期去重）。

        用于"连续模拟盘"阶段判断是否已积累足够交易日的真实下单记录。
        """
        with database.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS filled_orders,
                       COUNT(DISTINCT substr(filled_at, 1, 10)) AS trading_days,
                       MIN(filled_at) AS first_fill,
                       MAX(filled_at) AS last_fill
                FROM paper_fills
                """
            ).fetchone()
        return dict(row)


store = Store()
