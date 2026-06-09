import math
import random
from datetime import date, timedelta
from typing import Any, Dict, List

from quantdev.db import database
from quantdev.store import utc_now

DEMO_INSTRUMENTS = [
    ("000001.SZ", "平安银行", "SZSE", "stock", "银行", 12.0, 0.00010, 0.016),
    ("000333.SZ", "美的集团", "SZSE", "stock", "家电", 55.0, 0.00022, 0.014),
    ("000858.SZ", "五粮液", "SZSE", "stock", "食品饮料", 145.0, 0.00016, 0.018),
    ("300750.SZ", "宁德时代", "SZSE", "stock", "电力设备", 190.0, 0.00030, 0.024),
    ("600036.SH", "招商银行", "SSE", "stock", "银行", 34.0, 0.00018, 0.015),
    ("600519.SH", "贵州茅台", "SSE", "stock", "食品饮料", 1600.0, 0.00020, 0.013),
    ("601318.SH", "中国平安", "SSE", "stock", "非银金融", 46.0, 0.00012, 0.017),
    ("601899.SH", "紫金矿业", "SSE", "stock", "有色金属", 16.0, 0.00028, 0.021),
]

FACTOR_DEFINITIONS = [
    (
        "momentum_20",
        "20日动量",
        "过去20个交易日的价格涨幅，捕捉中短期趋势延续。",
        "价量",
        "close / delay(close, 20) - 1",
    ),
    (
        "reversal_5",
        "5日反转",
        "过去5个交易日收益率的相反数，捕捉短期过度反应。",
        "价量",
        "-(close / delay(close, 5) - 1)",
    ),
    (
        "low_volatility_20",
        "20日低波动",
        "20日收益波动率的相反数，偏好更稳定的资产。",
        "风险",
        "-std(return_1d, 20)",
    ),
    (
        "volume_ratio_20",
        "量能变化",
        "近5日成交量相对20日均量的变化。",
        "流动性",
        "mean(volume, 5) / mean(volume, 20) - 1",
    ),
    (
        "composite_multi",
        "多因子合成",
        "低波(0.6)与反转(0.4)经截面去极值标准化后加权合成的多因子模型。",
        "合成",
        "0.6*zscore(low_volatility_20) + 0.4*zscore(reversal_5)",
    ),
]


def trading_days(start: date, count: int) -> List[date]:
    result: List[date] = []
    cursor = start
    while len(result) < count:
        if cursor.weekday() < 5:
            result.append(cursor)
        cursor += timedelta(days=1)
    return result


class MarketDataService:
    demo_snapshot_id = "demo-cn-equity-20260605-v1"

    def ensure_factor_definitions(self) -> None:
        with database.transaction() as connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO factor_definitions
                    (factor_id, name, description, category, expression, version,
                     status, created_at)
                VALUES (?, ?, ?, ?, ?, '1.0.0', 'approved', ?)
                """,
                [item + (utc_now(),) for item in FACTOR_DEFINITIONS],
            )

    def reset_for_real_data(self) -> Dict[str, Any]:
        database.migrate()
        with database.transaction() as connection:
            for table in (
                "paper_fills",
                "paper_orders",
                "paper_positions",
                "paper_accounts",
                "backtest_runs",
                "factor_runs",
                "risk_events",
                "audit_log",
                "index_constituents",
                "financial_indicators",
                "daily_indicators",
                "prices",
                "instrument_metadata",
                "trade_calendar",
                "data_sync_runs",
                "indices",
                "instruments",
            ):
                connection.execute(f"DELETE FROM {table}")
        self.ensure_factor_definitions()
        return {
            "status": "ready",
            "database": str(database.path),
            "message": "研究数据与模拟账本已清空，可以同步真实数据",
        }

    def bootstrap_demo_data(self, force: bool = False) -> Dict[str, Any]:
        database.migrate()
        self.ensure_factor_definitions()
        with database.connect() as connection:
            existing = connection.execute("SELECT COUNT(*) AS count FROM prices").fetchone()
        if existing and existing["count"] and not force:
            return {
                "status": "ready",
                "snapshot_id": self.demo_snapshot_id,
                "rows": int(existing["count"]),
                "message": "样例数据已存在",
            }

        days = trading_days(date(2025, 1, 2), 370)
        price_rows = []
        for symbol_index, instrument in enumerate(DEMO_INSTRUMENTS):
            symbol, _, _, _, _, start_price, drift, volatility = instrument
            rng = random.Random(20260607 + symbol_index * 97)
            close = start_price
            volume_base = 8_000_000 + symbol_index * 1_250_000
            for day_index, trade_day in enumerate(days):
                market_cycle = math.sin(day_index / 31 + symbol_index) * 0.0017
                sector_cycle = math.cos(day_index / 53 + symbol_index / 2) * 0.0012
                daily_return = drift + market_cycle + sector_cycle + rng.gauss(0, volatility)
                previous_close = close
                close = max(1.0, previous_close * math.exp(daily_return))
                open_price = previous_close * (1 + rng.gauss(0, volatility / 4))
                high = max(open_price, close) * (1 + abs(rng.gauss(0, volatility / 3)))
                low = min(open_price, close) * (1 - abs(rng.gauss(0, volatility / 3)))
                volume = max(
                    100_000,
                    volume_base
                    * (1 + abs(daily_return) * 11)
                    * (1 + rng.gauss(0, 0.18)),
                )
                price_rows.append(
                    (
                        symbol,
                        trade_day.isoformat(),
                        round(open_price, 4),
                        round(high, 4),
                        round(max(0.01, low), 4),
                        round(close, 4),
                        round(volume, 2),
                        round(volume * close, 2),
                        1.0,
                        "demo-generator",
                        self.demo_snapshot_id,
                    )
                )

        with database.transaction() as connection:
            if force:
                connection.execute("DELETE FROM paper_fills")
                connection.execute("DELETE FROM paper_orders")
                connection.execute("DELETE FROM paper_positions")
                connection.execute("DELETE FROM paper_accounts")
                connection.execute("DELETE FROM backtest_runs")
                connection.execute("DELETE FROM factor_runs")
                connection.execute("DELETE FROM index_constituents")
                connection.execute("DELETE FROM financial_indicators")
                connection.execute("DELETE FROM daily_indicators")
                connection.execute("DELETE FROM prices")
                connection.execute("DELETE FROM instrument_metadata")
                connection.execute("DELETE FROM instruments")
                connection.execute("DELETE FROM indices")
                connection.execute("DELETE FROM trade_calendar")
                connection.execute("DELETE FROM data_sync_runs")
            connection.executemany(
                """
                INSERT OR REPLACE INTO instruments
                    (symbol, name, exchange, asset_type, industry, active)
                VALUES (?, ?, ?, ?, ?, 1)
                """,
                [item[:5] for item in DEMO_INSTRUMENTS],
            )
            connection.executemany(
                """
                INSERT OR REPLACE INTO prices
                    (symbol, trade_date, open, high, low, close, volume, amount,
                     adj_factor, source, snapshot_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                price_rows,
            )
        return {
            "status": "created",
            "snapshot_id": self.demo_snapshot_id,
            "rows": len(price_rows),
            "instruments": len(DEMO_INSTRUMENTS),
            "date_range": [days[0].isoformat(), days[-1].isoformat()],
            "message": "已生成可复现的 A 股样例数据",
        }


market_data_service = MarketDataService()
