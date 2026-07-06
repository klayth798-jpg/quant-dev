from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from quantdev.config import settings
from quantdev.models import QuoteUpdateRequest
from quantdev.services.calendar import CN_TZ, trading_calendar_service
from quantdev.store import store, utc_now


@dataclass(frozen=True)
class QuoteSnapshot:
    symbol: str
    price: Optional[float]
    bid: Optional[float]
    ask: Optional[float]
    quote_time: Optional[str]
    trade_date: Optional[str]
    source: str
    status: str
    fresh: bool
    age_seconds: Optional[float]
    reason: str


class QuoteService:
    def ingest(self, request: QuoteUpdateRequest) -> Dict[str, Any]:
        quote_time = request.quote_time
        if quote_time.tzinfo is None:
            quote_time = quote_time.replace(tzinfo=CN_TZ)
        quote_time = quote_time.astimezone(timezone.utc)
        now = datetime.now(timezone.utc)
        if quote_time > now.replace(microsecond=0) and (
            quote_time - now
        ).total_seconds() > 2:
            raise ValueError("行情时间不能明显晚于服务器时间")
        if request.bid is not None and request.ask is not None:
            if request.bid > request.ask:
                raise ValueError("行情买一价不能高于卖一价")
            spread_bps = (request.ask - request.bid) / request.price * 10_000
            if spread_bps > settings.quote_max_spread_bps * 10:
                raise ValueError("行情买卖价差异常，拒绝写入")
        if request.status.lower() not in {"tradable", "halted", "closed"}:
            raise ValueError("行情状态必须是 tradable、halted 或 closed")
        if not any(
            item["symbol"] == request.symbol for item in store.list_instruments()
        ):
            raise ValueError("未知标的，必须先同步证券主数据")
        payload = {
            "symbol": request.symbol,
            "price": float(request.price),
            "bid": float(request.bid) if request.bid is not None else None,
            "ask": float(request.ask) if request.ask is not None else None,
            "quote_time": quote_time.isoformat(),
            "trade_date": quote_time.astimezone(CN_TZ).date().isoformat(),
            "source": request.source,
            "status": request.status.lower(),
            "received_at": utc_now(),
        }
        store.upsert_market_quote(payload)
        return asdict(self.get(request.symbol))

    def get(
        self, symbol: str, now: Optional[datetime] = None
    ) -> QuoteSnapshot:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now = now.astimezone(timezone.utc)
        row = store.get_market_quote(symbol)
        if row:
            quote_time = datetime.fromisoformat(row["quote_time"]).astimezone(
                timezone.utc
            )
            age = (now - quote_time).total_seconds()
            fresh = (
                -2 <= age <= settings.quote_max_age_seconds
                and row["status"] == "tradable"
            )
            reason = ""
            if row["status"] != "tradable":
                reason = "行情状态不可交易：{}".format(row["status"])
            elif age < -2:
                reason = "行情时间位于未来：{:.1f} 秒".format(abs(age))
            elif not fresh:
                reason = "行情已过期：{:.1f} 秒".format(max(age, 0))
            return QuoteSnapshot(
                symbol=symbol,
                price=float(row["price"]),
                bid=float(row["bid"]) if row["bid"] is not None else None,
                ask=float(row["ask"]) if row["ask"] is not None else None,
                quote_time=row["quote_time"],
                trade_date=row["trade_date"],
                source=row["source"],
                status=row["status"],
                fresh=fresh,
                age_seconds=round(age, 3),
                reason=reason,
            )
        if settings.paper_quote_mode == "eod":
            return self._eod_quote(symbol, now)
        return QuoteSnapshot(
            symbol=symbol,
            price=None,
            bid=None,
            ask=None,
            quote_time=None,
            trade_date=None,
            source="",
            status="missing",
            fresh=False,
            age_seconds=None,
            reason="缺少实时行情",
        )

    def _eod_quote(self, symbol: str, now: datetime) -> QuoteSnapshot:
        snapshot_id = store.latest_snapshot_id()
        tail = (
            store.symbol_price_tail(symbol, snapshot_id, limit=1)
            if snapshot_id
            else []
        )
        expected = trading_calendar_service.latest_completed_trading_day(
            now.astimezone(CN_TZ)
        )
        if not tail:
            reason = "没有可用的日线行情"
        elif expected is None:
            reason = "交易日历未覆盖当前日期"
        elif tail[0]["trade_date"] != expected.isoformat():
            reason = "日线行情过期：最新 {}，应为 {}".format(
                tail[0]["trade_date"], expected.isoformat()
            )
        else:
            return QuoteSnapshot(
                symbol=symbol,
                price=float(tail[0]["close"]),
                bid=None,
                ask=None,
                quote_time=None,
                trade_date=tail[0]["trade_date"],
                source="eod-close",
                status="tradable",
                fresh=True,
                age_seconds=None,
                reason="",
            )
        return QuoteSnapshot(
            symbol=symbol,
            price=float(tail[0]["close"]) if tail else None,
            bid=None,
            ask=None,
            quote_time=None,
            trade_date=tail[0]["trade_date"] if tail else None,
            source="eod-close",
            status="stale",
            fresh=False,
            age_seconds=None,
            reason=reason,
        )

    def valuation_prices(self) -> Dict[str, float]:
        prices = store.latest_prices()
        now = datetime.now(timezone.utc)
        today = now.astimezone(CN_TZ).date().isoformat()
        for row in store.list_market_quotes():
            snapshot = self.get(row["symbol"], now=now)
            if snapshot.fresh and snapshot.trade_date == today:
                prices[row["symbol"]] = float(row["price"])
        return prices


quote_service = QuoteService()
