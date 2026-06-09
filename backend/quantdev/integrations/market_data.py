import time
from abc import ABC, abstractmethod
from threading import Lock
from typing import Any, Dict, Iterable, List, Optional

from quantdev.config import settings


class MarketDataAdapter(ABC):
    @abstractmethod
    def fetch_daily_bars(
        self, start_date: str, end_date: str
    ) -> Iterable[Dict[str, Any]]:
        raise NotImplementedError


class TushareAdapter(MarketDataAdapter):
    def __init__(
        self,
        token: str,
        call_interval_seconds: float = settings.tushare_call_interval_seconds,
        max_retries: int = 3,
    ):
        if not token:
            raise ValueError("缺少 TUSHARE_TOKEN")
        self.token = token
        self.call_interval_seconds = call_interval_seconds
        self.max_retries = max_retries
        self._last_call_at = 0.0
        self._throttle_lock = Lock()
        sdk_name = settings.market_data_sdk
        if sdk_name not in {"tushare", "tinyshare"}:
            raise ValueError("MARKET_DATA_SDK 只能是 tushare 或 tinyshare")
        try:
            if sdk_name == "tinyshare":
                import tinyshare as ts
            else:
                import tushare as ts
        except ImportError as exc:
            raise RuntimeError(
                f"缺少 {sdk_name} 依赖，请先安装对应行情 SDK"
            ) from exc
        ts.set_token(token)
        self.pro = ts.pro_api()

    def _throttle(self) -> None:
        with self._throttle_lock:
            elapsed = time.monotonic() - self._last_call_at
            remaining = self.call_interval_seconds - elapsed
            if remaining > 0:
                time.sleep(remaining)
            self._last_call_at = time.monotonic()

    def _call(self, endpoint: str, **kwargs) -> List[Dict[str, Any]]:
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                self._throttle()
                frame = getattr(self.pro, endpoint)(**kwargs)
                if frame is None or frame.empty:
                    return []
                return frame.where(frame.notna(), None).to_dict(orient="records")
            except Exception as exc:
                last_error = exc
                if attempt + 1 < self.max_retries:
                    time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(
            f"{settings.market_data_sdk} {endpoint} 调用失败: {last_error}"
        ) from last_error

    def fetch_stock_basic(self, list_status: str) -> List[Dict[str, Any]]:
        return self._call(
            "stock_basic",
            exchange="",
            list_status=list_status,
            fields=(
                "ts_code,symbol,name,area,industry,market,exchange,curr_type,"
                "list_status,list_date,delist_date,is_hs"
            ),
        )

    def fetch_trade_calendar(
        self, start_date: str, end_date: str
    ) -> List[Dict[str, Any]]:
        return self._call(
            "trade_cal",
            exchange="SSE",
            start_date=start_date,
            end_date=end_date,
            fields="exchange,cal_date,is_open,pretrade_date",
        )

    def fetch_daily_bars(
        self, start_date: str, end_date: str
    ) -> Iterable[Dict[str, Any]]:
        return self._call(
            "daily",
            start_date=start_date,
            end_date=end_date,
            fields="ts_code,trade_date,open,high,low,close,vol,amount",
        )

    def fetch_daily_bars_by_date(self, trade_date: str) -> List[Dict[str, Any]]:
        return self._call(
            "daily",
            trade_date=trade_date,
            fields="ts_code,trade_date,open,high,low,close,vol,amount",
        )

    def fetch_adj_factors_by_date(self, trade_date: str) -> List[Dict[str, Any]]:
        return self._call(
            "adj_factor",
            trade_date=trade_date,
            fields="ts_code,trade_date,adj_factor",
        )

    def fetch_daily_indicators_by_date(
        self, trade_date: str
    ) -> List[Dict[str, Any]]:
        return self._call(
            "daily_basic",
            trade_date=trade_date,
            fields=(
                "ts_code,trade_date,turnover_rate,volume_ratio,pe,pe_ttm,pb,"
                "ps_ttm,dv_ttm,total_share,float_share,free_share,total_mv,circ_mv"
            ),
        )

    def fetch_financial_indicators(
        self, symbol: str, start_date: str, end_date: str
    ) -> List[Dict[str, Any]]:
        return self._call(
            "fina_indicator",
            ts_code=symbol,
            start_date=start_date,
            end_date=end_date,
            fields=FINANCIAL_FIELDS,
        )

    def fetch_financial_indicators_vip(
        self, period: str
    ) -> List[Dict[str, Any]]:
        return self._call(
            "fina_indicator_vip",
            period=period,
            fields=FINANCIAL_FIELDS,
        )

    def fetch_index_basic(self, market: str) -> List[Dict[str, Any]]:
        return self._call(
            "index_basic",
            market=market,
            fields=(
                "ts_code,name,fullname,market,publisher,index_type,category,"
                "base_date,base_point,list_date,weight_rule,desc,exp_date"
            ),
        )

    def fetch_index_weights(
        self, index_code: str, start_date: str, end_date: str
    ) -> List[Dict[str, Any]]:
        return self._call(
            "index_weight",
            index_code=index_code,
            start_date=start_date,
            end_date=end_date,
            fields="index_code,con_code,trade_date,weight",
        )


FINANCIAL_FIELDS = (
    "ts_code,ann_date,end_date,eps,bps,roe,roa,roic,grossprofit_margin,"
    "netprofit_margin,debt_to_assets,current_ratio,quick_ratio,ocfps,"
    "netprofit_yoy,tr_yoy,or_yoy,q_netprofit_yoy,q_sales_yoy,update_flag"
)
