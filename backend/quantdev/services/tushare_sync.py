import json
import math
from calendar import monthrange
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from quantdev.config import settings
from quantdev.db import database
from quantdev.integrations.market_data import TushareAdapter
from quantdev.models import TushareSyncRequest
from quantdev.store import new_id, store, utc_now


def _api_date(value: date) -> str:
    return value.strftime("%Y%m%d")


def _iso_date(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    raw = str(value)
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    return raw


def _number(value: Any, multiplier: float = 1.0) -> Optional[float]:
    if value in (None, ""):
        return None
    result = float(value) * multiplier
    return result if math.isfinite(result) else None


def _month_windows(start: date, end: date) -> Iterable[Tuple[date, date]]:
    cursor = start.replace(day=1)
    while cursor <= end:
        last = date(cursor.year, cursor.month, monthrange(cursor.year, cursor.month)[1])
        yield max(start, cursor), min(end, last)
        cursor = (
            date(cursor.year + 1, 1, 1)
            if cursor.month == 12
            else date(cursor.year, cursor.month + 1, 1)
        )


def _quarter_ends(start: date, end: date) -> List[date]:
    result = []
    for year in range(start.year, end.year + 1):
        for month, day in ((3, 31), (6, 30), (9, 30), (12, 31)):
            period = date(year, month, day)
            if start <= period <= end:
                result.append(period)
    return result


class TushareSyncService:
    def __init__(
        self,
        adapter_factory: Callable[[str], TushareAdapter] = TushareAdapter,
    ):
        self.adapter_factory = adapter_factory

    @property
    def provider(self) -> str:
        return settings.market_data_sdk

    @property
    def snapshot_id(self) -> str:
        return f"{self.provider}-cn-equity-live-v1"

    @staticmethod
    def _ensure_instruments(connection, symbols: Iterable[str]) -> None:
        unique_symbols = sorted(set(symbols))
        connection.executemany(
            """
            INSERT OR IGNORE INTO instruments
                (symbol, name, exchange, asset_type, industry, active)
            VALUES (?, ?, ?, 'stock', '未分类', 1)
            """,
            [
                (
                    symbol,
                    symbol,
                    "SSE" if symbol.endswith(".SH") else "SZSE",
                )
                for symbol in unique_symbols
            ],
        )

    def create_run(self, request: TushareSyncRequest) -> Dict[str, Any]:
        if request.end_date < request.start_date:
            raise ValueError("结束日期不能早于开始日期")
        run_id = new_id("sync")
        parameters = request.model_dump(mode="json")
        with database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO data_sync_runs
                    (run_id, provider, dataset, status, parameters_json, stats_json,
                     error_message, started_at, finished_at)
                VALUES (?, ?, 'cn_equity', 'queued', ?, '{}', NULL, ?, NULL)
                """,
                (
                    run_id,
                    self.provider,
                    json.dumps(parameters, ensure_ascii=True),
                    utc_now(),
                ),
            )
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> Dict[str, Any]:
        result = store.get_data_sync_run(run_id)
        if not result:
            raise ValueError("同步任务不存在")
        return result

    def run(
        self,
        run_id: str,
        request: TushareSyncRequest,
        adapter: Optional[TushareAdapter] = None,
    ) -> Dict[str, Any]:
        self._set_run_state(run_id, "running", {}, None, finished=False)
        stats: Dict[str, Any] = {
            "snapshot_id": self.snapshot_id,
            "instruments": 0,
            "calendar_days": 0,
            "price_rows": 0,
            "daily_indicator_rows": 0,
            "financial_rows": 0,
            "index_rows": 0,
            "constituent_rows": 0,
            "skipped_price_dates": 0,
            "skipped_indicator_dates": 0,
        }
        try:
            client = adapter or self.adapter_factory(settings.tushare_token)
            stats["instruments"] = self._sync_instruments(client)
            calendar = self._sync_calendar(client, request.start_date, request.end_date)
            stats["calendar_days"] = len(calendar)
            if request.sync_daily:
                daily_stats = self._sync_daily(client, calendar, run_id)
                stats.update(daily_stats)
            if request.sync_indices:
                index_stats = self._sync_indices(client, request, run_id)
                stats.update(index_stats)
            if request.sync_financials:
                stats["financial_rows"] = self._sync_financials(
                    client, request, run_id
                )
            self._set_run_state(run_id, "completed", stats, None, finished=True)
            store.audit(
                actor="local-user",
                action="sync_market_data",
                resource_type="data_sync_run",
                resource_id=run_id,
                payload=stats,
            )
        except KeyboardInterrupt:
            stats["failed_at"] = utc_now()
            self._set_run_state(
                run_id,
                "interrupted",
                stats,
                "同步任务被手动中断，可重新运行并从已完成日期继续",
                finished=True,
            )
            raise
        except Exception as exc:
            stats["failed_at"] = utc_now()
            self._set_run_state(
                run_id,
                "failed",
                stats,
                str(exc)[:2000],
                finished=True,
            )
        return self.get_run(run_id)

    def _set_run_state(
        self,
        run_id: str,
        status: str,
        stats: Dict[str, Any],
        error: Optional[str],
        finished: bool,
    ) -> None:
        with database.transaction() as connection:
            connection.execute(
                """
                UPDATE data_sync_runs
                SET status = ?, stats_json = ?, error_message = ?, finished_at = ?
                WHERE run_id = ?
                """,
                (
                    status,
                    json.dumps(stats, ensure_ascii=True),
                    error,
                    utc_now() if finished else None,
                    run_id,
                ),
            )

    def _sync_instruments(self, adapter: TushareAdapter) -> int:
        rows: List[Dict[str, Any]] = []
        for list_status in ("L", "D", "P"):
            rows.extend(adapter.fetch_stock_basic(list_status))
        now = utc_now()
        with database.transaction() as connection:
            for row in rows:
                symbol = str(row["ts_code"])
                exchange = {
                    "SSE": "SSE",
                    "SZSE": "SZSE",
                    "BSE": "BSE",
                }.get(str(row.get("exchange") or ""), str(row.get("exchange") or "CN"))
                connection.execute(
                    """
                    INSERT INTO instruments
                        (symbol, name, exchange, asset_type, industry, active)
                    VALUES (?, ?, ?, 'stock', ?, ?)
                    ON CONFLICT(symbol) DO UPDATE SET
                        name = excluded.name,
                        exchange = excluded.exchange,
                        industry = excluded.industry,
                        active = excluded.active
                    """,
                    (
                        symbol,
                        str(row.get("name") or symbol),
                        exchange,
                        str(row.get("industry") or "未分类"),
                        1 if row.get("list_status") == "L" else 0,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO instrument_metadata
                        (symbol, raw_symbol, area, market, currency, list_status,
                         list_date, delist_date, is_hs, source, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol) DO UPDATE SET
                        raw_symbol = excluded.raw_symbol,
                        area = excluded.area,
                        market = excluded.market,
                        currency = excluded.currency,
                        list_status = excluded.list_status,
                        list_date = excluded.list_date,
                        delist_date = excluded.delist_date,
                        is_hs = excluded.is_hs,
                        updated_at = excluded.updated_at
                    """,
                    (
                        symbol,
                        str(row.get("symbol") or symbol.split(".")[0]),
                        row.get("area"),
                        row.get("market"),
                        row.get("curr_type"),
                        str(row.get("list_status") or ""),
                        _iso_date(row.get("list_date")),
                        _iso_date(row.get("delist_date")),
                        row.get("is_hs"),
                        self.provider,
                        now,
                    ),
                )
        return len(rows)

    def _sync_calendar(
        self, adapter: TushareAdapter, start: date, end: date
    ) -> List[Dict[str, Any]]:
        rows = adapter.fetch_trade_calendar(_api_date(start), _api_date(end))
        now = utc_now()
        with database.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO trade_calendar
                    (exchange, cal_date, is_open, pretrade_date, source, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(exchange, cal_date) DO UPDATE SET
                    is_open = excluded.is_open,
                    pretrade_date = excluded.pretrade_date,
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        str(row.get("exchange") or "SSE"),
                        _iso_date(row["cal_date"]),
                        int(row.get("is_open") or 0),
                        _iso_date(row.get("pretrade_date")),
                        self.provider,
                        now,
                    )
                    for row in rows
                ],
            )
        return rows

    def _sync_daily(
        self,
        adapter: TushareAdapter,
        calendar: List[Dict[str, Any]],
        run_id: str,
    ) -> Dict[str, int]:
        open_dates = [
            str(row["cal_date"])
            for row in calendar
            if int(row.get("is_open") or 0) == 1
        ]
        with database.connect() as connection:
            existing_prices = {
                row["trade_date"].replace("-", "")
                for row in connection.execute(
                    """
                    SELECT DISTINCT trade_date FROM prices
                    WHERE source = ? AND snapshot_id = ?
                    """,
                    (self.provider, self.snapshot_id),
                ).fetchall()
            }
            existing_indicators = {
                row["trade_date"].replace("-", "")
                for row in connection.execute(
                    "SELECT DISTINCT trade_date FROM daily_indicators "
                    "WHERE source = ?",
                    (self.provider,),
                ).fetchall()
            }

        result = {
            "price_rows": 0,
            "daily_indicator_rows": 0,
            "skipped_price_dates": 0,
            "skipped_indicator_dates": 0,
        }
        jobs = []
        for trade_date in sorted(open_dates):
            needs_prices = trade_date not in existing_prices
            needs_indicators = trade_date not in existing_indicators
            if not needs_prices:
                result["skipped_price_dates"] += 1
            if not needs_indicators:
                result["skipped_indicator_dates"] += 1
            if needs_prices or needs_indicators:
                jobs.append((trade_date, needs_prices, needs_indicators))

        with ThreadPoolExecutor(max_workers=8) as executor:
            bundles = executor.map(
                lambda job: self._fetch_daily_bundle(adapter, *job),
                jobs,
            )
            for bundle in bundles:
                if bundle["needs_prices"]:
                    result["price_rows"] += self._save_prices(
                        bundle["bars"],
                        bundle["factors"],
                    )
                if bundle["needs_indicators"]:
                    result["daily_indicator_rows"] += self._save_daily_indicators(
                        bundle["indicators"],
                        run_id,
                    )
        return result

    @staticmethod
    def _fetch_daily_bundle(
        adapter: TushareAdapter,
        trade_date: str,
        needs_prices: bool,
        needs_indicators: bool,
    ) -> Dict[str, Any]:
        bundle: Dict[str, Any] = {
            "trade_date": trade_date,
            "needs_prices": needs_prices,
            "needs_indicators": needs_indicators,
        }
        if needs_prices:
            bundle["bars"] = adapter.fetch_daily_bars_by_date(trade_date)
            bundle["factors"] = {
                row["ts_code"]: row.get("adj_factor")
                for row in adapter.fetch_adj_factors_by_date(trade_date)
            }
        if needs_indicators:
            bundle["indicators"] = adapter.fetch_daily_indicators_by_date(trade_date)
        return bundle

    def _save_prices(
        self,
        bars: List[Dict[str, Any]],
        factors: Dict[str, Any],
    ) -> int:
        values = []
        for row in bars:
            required = [_number(row.get(key)) for key in ("open", "high", "low", "close")]
            if any(value is None for value in required):
                continue
            symbol = str(row["ts_code"])
            values.append(
                (
                    symbol,
                    _iso_date(row["trade_date"]),
                    *required,
                    _number(row.get("vol"), 100) or 0.0,
                    _number(row.get("amount"), 1000) or 0.0,
                    _number(factors.get(symbol)) or 1.0,
                    self.provider,
                    self.snapshot_id,
                )
            )
        with database.transaction() as connection:
            self._ensure_instruments(
                connection,
                (value[0] for value in values),
            )
            connection.executemany(
                """
                INSERT INTO prices
                    (symbol, trade_date, open, high, low, close, volume, amount,
                     adj_factor, source, snapshot_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, trade_date, snapshot_id) DO UPDATE SET
                    open = excluded.open,
                    high = excluded.high,
                    low = excluded.low,
                    close = excluded.close,
                    volume = excluded.volume,
                    amount = excluded.amount,
                    adj_factor = excluded.adj_factor
                """,
                values,
            )
        return len(values)

    def _save_daily_indicators(
        self, rows: List[Dict[str, Any]], run_id: str
    ) -> int:
        values = []
        for row in rows:
            values.append(
                (
                    str(row["ts_code"]),
                    _iso_date(row["trade_date"]),
                    _number(row.get("turnover_rate")),
                    _number(row.get("volume_ratio")),
                    _number(row.get("pe")),
                    _number(row.get("pe_ttm")),
                    _number(row.get("pb")),
                    _number(row.get("ps_ttm")),
                    _number(row.get("dv_ttm")),
                    _number(row.get("total_share"), 10_000),
                    _number(row.get("float_share"), 10_000),
                    _number(row.get("free_share"), 10_000),
                    _number(row.get("total_mv"), 10_000),
                    _number(row.get("circ_mv"), 10_000),
                    self.provider,
                    run_id,
                )
            )
        with database.transaction() as connection:
            self._ensure_instruments(
                connection,
                (value[0] for value in values),
            )
            connection.executemany(
                """
                INSERT INTO daily_indicators
                    (symbol, trade_date, turnover_rate, volume_ratio, pe, pe_ttm, pb,
                     ps_ttm, dv_ttm, total_share, float_share, free_share, total_mv,
                     circ_mv, source, sync_run_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, trade_date) DO UPDATE SET
                    turnover_rate = excluded.turnover_rate,
                    volume_ratio = excluded.volume_ratio,
                    pe = excluded.pe,
                    pe_ttm = excluded.pe_ttm,
                    pb = excluded.pb,
                    ps_ttm = excluded.ps_ttm,
                    dv_ttm = excluded.dv_ttm,
                    total_share = excluded.total_share,
                    float_share = excluded.float_share,
                    free_share = excluded.free_share,
                    total_mv = excluded.total_mv,
                    circ_mv = excluded.circ_mv,
                    sync_run_id = excluded.sync_run_id
                """,
                values,
            )
        return len(values)

    def _sync_indices(
        self,
        adapter: TushareAdapter,
        request: TushareSyncRequest,
        run_id: str,
    ) -> Dict[str, int]:
        basic_rows: List[Dict[str, Any]] = []
        for market in ("SSE", "SZSE", "CSI"):
            basic_rows.extend(adapter.fetch_index_basic(market))
        now = utc_now()
        with database.transaction() as connection:
            for row in basic_rows:
                connection.execute(
                    """
                    INSERT INTO indices
                        (index_code, name, fullname, market, publisher, index_type,
                         category, base_date, base_point, list_date, weight_rule,
                        description, exp_date, source, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(index_code) DO UPDATE SET
                        name = excluded.name,
                        fullname = excluded.fullname,
                        market = excluded.market,
                        publisher = excluded.publisher,
                        index_type = excluded.index_type,
                        category = excluded.category,
                        base_date = excluded.base_date,
                        base_point = excluded.base_point,
                        list_date = excluded.list_date,
                        weight_rule = excluded.weight_rule,
                        description = excluded.description,
                        exp_date = excluded.exp_date,
                        updated_at = excluded.updated_at
                    """,
                    (
                        str(row["ts_code"]),
                        str(row.get("name") or row["ts_code"]),
                        row.get("fullname"),
                        row.get("market"),
                        row.get("publisher"),
                        row.get("index_type"),
                        row.get("category"),
                        _iso_date(row.get("base_date")),
                        _number(row.get("base_point")),
                        _iso_date(row.get("list_date")),
                        row.get("weight_rule"),
                        row.get("desc"),
                        _iso_date(row.get("exp_date")),
                        self.provider,
                        now,
                    ),
                )

        index_codes = list(
            dict.fromkeys(request.indices or list(settings.tushare_default_indices))
        )
        constituent_rows = 0
        with database.transaction() as connection:
            for index_code in index_codes:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO indices
                        (index_code, name, source, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (index_code, index_code, self.provider, now),
                )

        for index_code in index_codes:
            for window_start, window_end in _month_windows(
                request.start_date, request.end_date
            ):
                with database.connect() as connection:
                    exists = connection.execute(
                        """
                        SELECT 1 FROM index_constituents
                        WHERE index_code = ? AND trade_date BETWEEN ? AND ?
                        LIMIT 1
                        """,
                        (
                            index_code,
                            window_start.isoformat(),
                            window_end.isoformat(),
                        ),
                    ).fetchone()
                if exists:
                    continue
                rows = adapter.fetch_index_weights(
                    index_code,
                    _api_date(window_start),
                    _api_date(window_end),
                )
                constituent_rows += self._save_index_weights(rows, run_id)
        return {
            "index_rows": len(basic_rows),
            "constituent_rows": constituent_rows,
        }

    def _save_index_weights(
        self, rows: List[Dict[str, Any]], run_id: str
    ) -> int:
        with database.transaction() as connection:
            for row in rows:
                symbol = str(row["con_code"])
                connection.execute(
                    """
                    INSERT OR IGNORE INTO instruments
                        (symbol, name, exchange, asset_type, industry, active)
                    VALUES (?, ?, ?, 'stock', '未分类', 1)
                    """,
                    (
                        symbol,
                        symbol,
                        "SSE" if symbol.endswith(".SH") else "SZSE",
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO index_constituents
                        (index_code, symbol, trade_date, weight, source, sync_run_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(index_code, symbol, trade_date) DO UPDATE SET
                        weight = excluded.weight,
                        sync_run_id = excluded.sync_run_id
                    """,
                    (
                        str(row["index_code"]),
                        symbol,
                        _iso_date(row["trade_date"]),
                        _number(row.get("weight")) or 0.0,
                        self.provider,
                        run_id,
                    ),
                )
        return len(rows)

    def _sync_financials(
        self,
        adapter: TushareAdapter,
        request: TushareSyncRequest,
        run_id: str,
    ) -> int:
        total = 0
        if settings.tushare_financial_vip and not request.financial_symbols:
            for period in _quarter_ends(request.start_date, request.end_date):
                rows = adapter.fetch_financial_indicators_vip(_api_date(period))
                total += self._save_financials(rows, run_id)
            return total

        symbols = request.financial_symbols or store.latest_constituent_symbols(
            request.indices or list(settings.tushare_default_indices)
        )
        if not symbols:
            symbols = [row["symbol"] for row in store.list_instruments()]
        symbols = list(dict.fromkeys(symbols))[
            : request.max_standard_financial_symbols
        ]
        for symbol in symbols:
            rows = adapter.fetch_financial_indicators(
                symbol,
                _api_date(request.start_date),
                _api_date(request.end_date),
            )
            total += self._save_financials(rows, run_id)
        return total

    def _save_financials(
        self, rows: List[Dict[str, Any]], run_id: str
    ) -> int:
        fields = (
            "eps",
            "bps",
            "roe",
            "roa",
            "roic",
            "grossprofit_margin",
            "netprofit_margin",
            "debt_to_assets",
            "current_ratio",
            "quick_ratio",
            "ocfps",
            "netprofit_yoy",
            "tr_yoy",
            "or_yoy",
            "q_netprofit_yoy",
            "q_sales_yoy",
        )
        values = []
        for row in rows:
            if not row.get("ts_code") or not row.get("ann_date") or not row.get("end_date"):
                continue
            values.append(
                (
                    str(row["ts_code"]),
                    _iso_date(row["ann_date"]),
                    _iso_date(row["end_date"]),
                    str(row.get("update_flag") or ""),
                    *[_number(row.get(field)) for field in fields],
                    run_id,
                    json.dumps(row, ensure_ascii=True, default=str),
                )
            )
        with database.transaction() as connection:
            self._ensure_instruments(
                connection,
                (value[0] for value in values),
            )
            connection.executemany(
                """
                INSERT INTO financial_indicators
                    (symbol, ann_date, end_date, update_flag, eps, bps, roe, roa, roic,
                     grossprofit_margin, netprofit_margin, debt_to_assets,
                     current_ratio, quick_ratio, ocfps, netprofit_yoy, tr_yoy, or_yoy,
                    q_netprofit_yoy, q_sales_yoy, source, sync_run_id, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?)
                ON CONFLICT(symbol, ann_date, end_date, update_flag) DO UPDATE SET
                    eps = excluded.eps,
                    bps = excluded.bps,
                    roe = excluded.roe,
                    roa = excluded.roa,
                    roic = excluded.roic,
                    grossprofit_margin = excluded.grossprofit_margin,
                    netprofit_margin = excluded.netprofit_margin,
                    debt_to_assets = excluded.debt_to_assets,
                    current_ratio = excluded.current_ratio,
                    quick_ratio = excluded.quick_ratio,
                    ocfps = excluded.ocfps,
                    netprofit_yoy = excluded.netprofit_yoy,
                    tr_yoy = excluded.tr_yoy,
                    or_yoy = excluded.or_yoy,
                    q_netprofit_yoy = excluded.q_netprofit_yoy,
                    q_sales_yoy = excluded.q_sales_yoy,
                    sync_run_id = excluded.sync_run_id,
                    raw_json = excluded.raw_json
                """,
                [value[:-2] + (self.provider,) + value[-2:] for value in values],
            )
        return len(values)

    def status(self) -> Dict[str, Any]:
        with database.connect() as connection:
            real_prices = connection.execute(
                """
                SELECT COUNT(*) AS row_count, COUNT(DISTINCT symbol) AS symbols,
                       MIN(trade_date) AS start_date, MAX(trade_date) AS end_date
                FROM prices WHERE source = ?
                """,
                (self.provider,),
            ).fetchone()
            financial_count = connection.execute(
                "SELECT COUNT(*) AS count FROM financial_indicators"
            ).fetchone()["count"]
            index_count = connection.execute(
                "SELECT COUNT(*) AS count FROM indices"
            ).fetchone()["count"]
            constituent_count = connection.execute(
                "SELECT COUNT(*) AS count FROM index_constituents"
            ).fetchone()["count"]
        return {
            "provider": self.provider,
            "configured": bool(settings.tushare_token),
            "financial_mode": (
                "vip" if settings.tushare_financial_vip else "standard"
            ),
            "snapshot_id": self.snapshot_id,
            "default_indices": list(settings.tushare_default_indices),
            "prices": dict(real_prices),
            "financial_rows": int(financial_count),
            "index_rows": int(index_count),
            "constituent_rows": int(constituent_count),
            "runs": store.list_data_sync_runs(limit=10),
        }


tushare_sync_service = TushareSyncService()
