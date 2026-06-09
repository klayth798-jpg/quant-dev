from datetime import date

from quantdev.db import database
from quantdev.models import TushareSyncRequest
from quantdev.services.tushare_sync import TushareSyncService


class FakeTushareAdapter:
    def fetch_stock_basic(self, list_status):
        if list_status != "L":
            return []
        return [
            {
                "ts_code": "000001.SZ",
                "symbol": "000001",
                "name": "平安银行",
                "area": "深圳",
                "industry": "银行",
                "market": "主板",
                "exchange": "SZSE",
                "curr_type": "CNY",
                "list_status": "L",
                "list_date": "19910403",
                "delist_date": None,
                "is_hs": "S",
            },
            {
                "ts_code": "600000.SH",
                "symbol": "600000",
                "name": "浦发银行",
                "area": "上海",
                "industry": "银行",
                "market": "主板",
                "exchange": "SSE",
                "curr_type": "CNY",
                "list_status": "L",
                "list_date": "19991110",
                "delist_date": None,
                "is_hs": "H",
            },
        ]

    def fetch_trade_calendar(self, start_date, end_date):
        assert (start_date, end_date) == ("20260601", "20260603")
        return [
            {
                "exchange": "SSE",
                "cal_date": "20260601",
                "is_open": 1,
                "pretrade_date": "20260529",
            },
            {
                "exchange": "SSE",
                "cal_date": "20260602",
                "is_open": 1,
                "pretrade_date": "20260601",
            },
            {
                "exchange": "SSE",
                "cal_date": "20260603",
                "is_open": 0,
                "pretrade_date": "20260602",
            },
        ]

    def fetch_daily_bars_by_date(self, trade_date):
        return [
            {
                "ts_code": "000001.SZ",
                "trade_date": trade_date,
                "open": 10,
                "high": 11,
                "low": 9.8,
                "close": 10.5,
                "vol": 123,
                "amount": 456,
            }
        ]

    def fetch_adj_factors_by_date(self, trade_date):
        return [
            {
                "ts_code": "000001.SZ",
                "trade_date": trade_date,
                "adj_factor": 2.5,
            }
        ]

    def fetch_daily_indicators_by_date(self, trade_date):
        return [
            {
                "ts_code": "000001.SZ",
                "trade_date": trade_date,
                "turnover_rate": 1.2,
                "volume_ratio": 0.9,
                "pe": 6.5,
                "pe_ttm": 6.7,
                "pb": 0.8,
                "ps_ttm": 1.1,
                "dv_ttm": 4.0,
                "total_share": 100,
                "float_share": 80,
                "free_share": 70,
                "total_mv": 1000,
                "circ_mv": 800,
            }
        ]

    def fetch_index_basic(self, market):
        if market != "CSI":
            return []
        return [
            {
                "ts_code": "000300.SH",
                "name": "沪深300",
                "fullname": "沪深300指数",
                "market": "CSI",
                "publisher": "中证公司",
                "index_type": "规模指数",
                "category": "综合指数",
                "base_date": "20041231",
                "base_point": 1000,
                "list_date": "20050408",
                "weight_rule": "自由流通市值",
                "desc": "测试指数",
                "exp_date": None,
            }
        ]

    def fetch_index_weights(self, index_code, start_date, end_date):
        assert index_code == "000300.SH"
        return [
            {
                "index_code": index_code,
                "con_code": "000001.SZ",
                "trade_date": "20260601",
                "weight": 0.45,
            }
        ]

    def fetch_financial_indicators(self, symbol, start_date, end_date):
        assert symbol == "000001.SZ"
        return [
            {
                "ts_code": symbol,
                "ann_date": "20260420",
                "end_date": "20260331",
                "eps": 0.5,
                "bps": 12.3,
                "roe": 3.2,
                "debt_to_assets": 91.0,
                "netprofit_yoy": 2.1,
                "update_flag": "1",
            }
        ]


def test_tushare_sync_persists_market_financial_and_index_data():
    service = TushareSyncService()
    request = TushareSyncRequest(
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 3),
        indices=["000300.SH"],
        financial_symbols=["000001.SZ"],
    )
    queued = service.create_run(request)
    result = service.run(queued["run_id"], request, adapter=FakeTushareAdapter())

    assert result["status"] == "completed"
    assert result["stats"]["price_rows"] == 2
    assert result["stats"]["daily_indicator_rows"] == 2
    assert result["stats"]["financial_rows"] == 1
    assert result["stats"]["constituent_rows"] == 1

    with database.connect() as connection:
        price = connection.execute(
            """
            SELECT * FROM prices
            WHERE symbol = '000001.SZ' AND source = ?
            ORDER BY trade_date LIMIT 1
            """,
            (service.provider,),
        ).fetchone()
        indicator = connection.execute(
            """
            SELECT * FROM daily_indicators
            WHERE symbol = '000001.SZ'
            ORDER BY trade_date LIMIT 1
            """
        ).fetchone()
        financial = connection.execute(
            "SELECT * FROM financial_indicators WHERE symbol = '000001.SZ'"
        ).fetchone()
        constituent = connection.execute(
            "SELECT * FROM index_constituents WHERE index_code = '000300.SH'"
        ).fetchone()

    assert price["trade_date"] == "2026-06-01"
    assert price["volume"] == 12_300
    assert price["amount"] == 456_000
    assert price["adj_factor"] == 2.5
    assert indicator["total_share"] == 1_000_000
    assert indicator["total_mv"] == 10_000_000
    assert financial["end_date"] == "2026-03-31"
    assert constituent["weight"] == 0.45


def test_tushare_sync_skips_existing_daily_dates():
    service = TushareSyncService()
    request = TushareSyncRequest(
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 3),
        sync_financials=False,
        sync_indices=False,
    )
    first = service.create_run(request)
    service.run(first["run_id"], request, adapter=FakeTushareAdapter())
    second = service.create_run(request)
    result = service.run(second["run_id"], request, adapter=FakeTushareAdapter())

    assert result["status"] == "completed"
    assert result["stats"]["price_rows"] == 0
    assert result["stats"]["daily_indicator_rows"] == 0
    assert result["stats"]["skipped_price_dates"] == 2
