"""可交易性校验：实盘下单前判断标的是否停牌、限价是否触及涨跌停板。

A股两类常见"下单即废"场景：
  1. 停牌：标的当日无行情（快照里该标的最新日期落后于全市场最新交易日），
     或基础信息中 list_status 为 P(暂停上市)/D(退市)。停牌期间一律拒单。
  2. 涨跌停限价：买单限价高于当日涨停价、卖单限价低于当日跌停价，
     在真实券商前端会被直接拒绝（价格越界）。

涨跌停幅度按板块/风险警示状态区分（与回测口径一致）：
  - ST：5%；创业板(300/301)/科创板(688/689)：20%；北交所(.BJ)：30%；主板：10%。
涨跌停价基于前收盘价（最近两交易日中较早一日的收盘）× 幅度，按分(0.01)四舍五入。
"""

from dataclasses import dataclass
from typing import Optional

from quantdev.store import store


@dataclass(frozen=True)
class Tradability:
    symbol: str
    halted: bool
    halt_reason: str
    previous_close: Optional[float]
    limit_up: Optional[float]
    limit_down: Optional[float]
    limit_ratio: float
    is_st: bool


class TradabilityService:
    @staticmethod
    def price_limit_ratio(symbol: str, is_st: bool) -> float:
        """按板块与风险警示状态返回每日涨跌停幅度（与回测一致）。"""
        if is_st:
            return 0.05
        if symbol.startswith(("300", "301")) or symbol.startswith(("688", "689")):
            return 0.20  # 创业板 / 科创板
        if symbol.startswith(("8", "4")) and symbol.endswith(".BJ"):
            return 0.30  # 北交所
        return 0.10  # 主板

    @staticmethod
    def _is_st(symbol: str) -> bool:
        for row in store.list_instruments():
            if row["symbol"] == symbol:
                return "ST" in str(row.get("name", "")).upper()
        return False

    def evaluate(self, symbol: str) -> Tradability:
        snapshot_id = store.latest_snapshot_id()
        is_st = self._is_st(symbol)
        ratio = self.price_limit_ratio(symbol, is_st)
        if not snapshot_id:
            return Tradability(
                symbol=symbol, halted=True, halt_reason="没有可用的数据快照",
                previous_close=None, limit_up=None, limit_down=None,
                limit_ratio=ratio, is_st=is_st,
            )
        market_date = store.snapshot_max_date(snapshot_id)
        tail = store.symbol_price_tail(symbol, snapshot_id, limit=2)
        if not tail:
            return Tradability(
                symbol=symbol, halted=True, halt_reason="标的没有可用行情",
                previous_close=None, limit_up=None, limit_down=None,
                limit_ratio=ratio, is_st=is_st,
            )
        symbol_latest_date = tail[0]["trade_date"]
        if market_date is not None and symbol_latest_date < market_date:
            # 全市场已有更新的交易日，但该标的最新行情停留在更早日期 -> 停牌。
            return Tradability(
                symbol=symbol, halted=True,
                halt_reason="疑似停牌：最新行情停留在 {}（全市场已到 {}）".format(
                    symbol_latest_date, market_date
                ),
                previous_close=None, limit_up=None, limit_down=None,
                limit_ratio=ratio, is_st=is_st,
            )
        # 前收盘：取倒序第二条（不足两条时退化用最新一条）。
        previous_close = float(tail[1]["close"] if len(tail) > 1 else tail[0]["close"])
        limit_up = round(previous_close * (1 + ratio), 2)
        limit_down = round(previous_close * (1 - ratio), 2)
        return Tradability(
            symbol=symbol, halted=False, halt_reason="",
            previous_close=round(previous_close, 4),
            limit_up=limit_up, limit_down=limit_down,
            limit_ratio=ratio, is_st=is_st,
        )

    def check_limit_price(
        self, symbol: str, side: str, limit_price: float
    ) -> Optional[str]:
        """校验限价是否越过涨跌停板，越界返回拒单原因，否则 None。"""
        info = self.evaluate(symbol)
        if info.halted:
            return info.halt_reason
        if info.limit_up is None or info.limit_down is None:
            return None
        if side == "buy" and limit_price > info.limit_up + 1e-6:
            return "买入限价 {:.2f} 高于当日涨停价 {:.2f}".format(
                limit_price, info.limit_up
            )
        if side == "sell" and limit_price < info.limit_down - 1e-6:
            return "卖出限价 {:.2f} 低于当日跌停价 {:.2f}".format(
                limit_price, info.limit_down
            )
        return None


tradability_service = TradabilityService()
