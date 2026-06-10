"""交易日历 / 交易时段：实盘下单前的时间闸。

A股不是 7×24 市场——非交易日、非交易时段下单只会被券商拒绝或排队到下一时段，
是实盘里常见的"幽灵订单"来源。本模块提供两类判定：

  1. 交易日：优先查 trade_calendar 表（来自 tushare 同步）；表中无该日记录时，
     退化为"工作日(周一~周五)"兜底，保证 demo 模式下也可用。
  2. 交易时段：A股连续竞价时段——上午 09:30–11:30、下午 13:00–15:00（北京时间 UTC+8）。

判定一律基于北京时间（Asia/Shanghai，固定 UTC+8），不依赖服务器本地时区。
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

from quantdev.store import store

CN_TZ = timezone(timedelta(hours=8))

MORNING_OPEN = time(9, 30)
MORNING_CLOSE = time(11, 30)
AFTERNOON_OPEN = time(13, 0)
AFTERNOON_CLOSE = time(15, 0)


@dataclass(frozen=True)
class MarketClock:
    timestamp: str
    is_trading_day: bool
    is_trading_session: bool
    session: str  # morning / lunch_break / afternoon / closed
    calendar_source: str  # calendar / weekday_fallback
    can_trade: bool


class TradingCalendarService:
    exchange = "SSE"

    def _now_cn(self) -> datetime:
        return datetime.now(CN_TZ)

    def today(self) -> date:
        """当前北京时间日期（供 T+1 等按交易日判定的逻辑使用）。"""
        return self._now_cn().date()

    def is_trading_day(self, day: Optional[date] = None) -> bool:
        day = day or self._now_cn().date()
        entry = store.calendar_entry(day.isoformat(), self.exchange)
        if entry is not None:
            return int(entry["is_open"]) == 1
        # 日历无记录时退化为工作日兜底。
        return day.weekday() < 5

    def _calendar_source(self, day: date) -> str:
        return (
            "calendar"
            if store.calendar_entry(day.isoformat(), self.exchange) is not None
            else "weekday_fallback"
        )

    def _session(self, moment: time) -> str:
        if MORNING_OPEN <= moment <= MORNING_CLOSE:
            return "morning"
        if MORNING_CLOSE < moment < AFTERNOON_OPEN:
            return "lunch_break"
        if AFTERNOON_OPEN <= moment <= AFTERNOON_CLOSE:
            return "afternoon"
        return "closed"

    def market_clock(self, moment: Optional[datetime] = None) -> MarketClock:
        moment = moment or self._now_cn()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=CN_TZ)
        moment = moment.astimezone(CN_TZ)
        day = moment.date()
        trading_day = self.is_trading_day(day)
        session = self._session(moment.time()) if trading_day else "closed"
        is_session = session in {"morning", "afternoon"}
        return MarketClock(
            timestamp=moment.isoformat(),
            is_trading_day=trading_day,
            is_trading_session=is_session,
            session=session,
            calendar_source=self._calendar_source(day),
            can_trade=trading_day and is_session,
        )


trading_calendar_service = TradingCalendarService()
