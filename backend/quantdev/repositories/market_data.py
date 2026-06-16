from typing import Any, Dict, List, Optional

from quantdev.db import database
from quantdev.repositories.base import decode_json


class MarketDataRepository:
    """MarketDataRepository：market_data 子域数据访问。"""

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

    def latest_prices(self, snapshot_id: Optional[str] = None) -> Dict[str, float]:
        snapshot_id = snapshot_id or self.latest_snapshot_id()
        if not snapshot_id:
            return {}
        with database.connect() as connection:
            rows = connection.execute(
                """
                SELECT p.symbol, p.close
                FROM prices p
                JOIN (
                    SELECT symbol, MAX(trade_date) AS max_date
                    FROM prices
                    WHERE snapshot_id = ?
                    GROUP BY symbol
                ) latest
                  ON latest.symbol = p.symbol
                 AND latest.max_date = p.trade_date
                WHERE p.snapshot_id = ?
                """,
                (snapshot_id, snapshot_id),
            ).fetchall()
        return {str(row["symbol"]): float(row["close"]) for row in rows}

    def recent_price_rows(
        self,
        snapshot_id: str,
        as_of_date: str,
        limit_per_symbol: int = 21,
        symbols: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        filters = "snapshot_id = ? AND trade_date <= ?"
        params: List[Any] = [snapshot_id, as_of_date]
        if symbols:
            placeholders = ",".join("?" for _ in symbols)
            filters += f" AND symbol IN ({placeholders})"
            params.extend(symbols)
        params.append(limit_per_symbol)
        with database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM (
                    SELECT p.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY symbol ORDER BY trade_date DESC
                           ) AS row_number
                    FROM prices p
                    WHERE {filters}
                )
                WHERE row_number <= ?
                ORDER BY symbol, trade_date
                """,
                params,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item.pop("row_number", None)
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

    def constituent_symbols_at(
        self, index_codes: List[str], trade_date: str
    ) -> List[str]:
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
                    AND c2.trade_date <= ?
              )
            ORDER BY c.symbol
        """
        with database.connect() as connection:
            rows = connection.execute(query, [*index_codes, trade_date]).fetchall()
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

    def latest_market_caps(
        self, trade_date: str, symbols: Optional[List[str]] = None
    ) -> Dict[str, float]:
        filters = "trade_date <= ? AND total_mv IS NOT NULL AND total_mv > 0"
        params: List[Any] = [trade_date]
        if symbols:
            placeholders = ",".join("?" for _ in symbols)
            filters += f" AND symbol IN ({placeholders})"
            params.extend(symbols)
        with database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT d.symbol, d.total_mv
                FROM daily_indicators d
                JOIN (
                    SELECT symbol, MAX(trade_date) AS max_date
                    FROM daily_indicators
                    WHERE {filters}
                    GROUP BY symbol
                ) latest
                  ON latest.symbol = d.symbol
                 AND latest.max_date = d.trade_date
                """,
                params,
            ).fetchall()
        return {str(row["symbol"]): float(row["total_mv"]) for row in rows}

    def trading_day_count(self, start_date: str, end_date: str) -> int:
        with database.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM trade_calendar
                WHERE exchange = 'SSE' AND is_open = 1
                  AND cal_date > ? AND cal_date <= ?
                """,
                (start_date, end_date),
            ).fetchone()
        return int(row["count"] or 0)

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
