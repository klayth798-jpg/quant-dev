from typing import Any, Dict

from quantdev.config import settings
from quantdev.integrations.broker import BrokerAdapter
from quantdev.models import PositionInput, RiskCheckRequest
from quantdev.services.calendar import trading_calendar_service
from quantdev.services.live_guard import live_guard
from quantdev.services.quotes import quote_service
from quantdev.services.risk import risk_service
from quantdev.services.tradability import tradability_service
from quantdev.store import store


class PreTradeRiskService:
    lot_size = 100

    def check(
        self,
        intent: Dict[str, Any],
        broker: BrokerAdapter,
        *,
        approved: bool,
    ) -> Dict[str, Any]:
        reasons = []
        side = str(intent["side"]).lower()
        order_type = str(intent["order_type"]).lower()
        quantity = int(intent["quantity"])
        if side not in {"buy", "sell"}:
            reasons.append("方向必须是 buy 或 sell")
        if order_type not in {"market", "limit"}:
            reasons.append("订单类型必须是 market 或 limit")
        if quantity <= 0 or quantity % self.lot_size:
            reasons.append("A股委托数量必须是 100 股的正整数倍")

        health = broker.health_check()
        if not health.healthy or health.mode not in {"live", "mock_live"}:
            reasons.append("真实 Broker 不可用：{}".format(health.message))

        quote = quote_service.get(intent["symbol"])
        today = trading_calendar_service.today().isoformat()
        if not quote.fresh or quote.price is None:
            reasons.append(quote.reason or "缺少新鲜行情")
        elif quote.trade_date != today:
            reasons.append(
                "行情交易日不匹配：行情={}，当前={}".format(
                    quote.trade_date, today
                )
            )
        if quote.bid is not None and quote.ask is not None:
            if quote.bid > quote.ask:
                reasons.append("行情买一价高于卖一价")
            elif quote.price:
                spread_bps = (quote.ask - quote.bid) / quote.price * 10_000
                if spread_bps > settings.quote_max_spread_bps:
                    reasons.append(
                        "买卖价差 {:.1f} bps 超过上限 {:.1f} bps".format(
                            spread_bps, settings.quote_max_spread_bps
                        )
                    )

        limit_price = intent.get("limit_price")
        if order_type == "limit" and not limit_price:
            reasons.append("限价单缺少 limit_price")
        tradability = tradability_service.evaluate(intent["symbol"])
        if tradability.halted:
            reasons.append(tradability.halt_reason)
        elif order_type == "limit" and limit_price:
            limit_reason = tradability_service.check_limit_price(
                intent["symbol"], side, float(limit_price)
            )
            if limit_reason:
                reasons.append(limit_reason)

        reference_price = float(
            limit_price
            or (
                quote.ask
                if side == "buy" and quote.ask is not None
                else quote.bid
                if side == "sell" and quote.bid is not None
                else quote.price
                or 0
            )
        )
        notional = reference_price * quantity
        guard = live_guard.check_live_order(notional, approved=approved)
        reasons.extend(guard.reasons)

        account = broker.get_account()
        positions = broker.get_positions()
        position = next(
            (item for item in positions if item.symbol == intent["symbol"]),
            None,
        )
        if side == "buy" and account.cash < notional:
            reasons.append(
                "券商可用资金不足：可用 {:.2f}，委托 {:.2f}".format(
                    account.cash, notional
                )
            )
        if side == "sell":
            sellable = (
                int(position.sellable_quantity)
                if position and position.sellable_quantity is not None
                else int(position.quantity)
                if position
                else 0
            )
            if sellable < quantity:
                reasons.append(
                    "券商可卖数量不足：可卖 {}，委托 {}".format(
                        sellable, quantity
                    )
                )

        store.sync_live_account_state(
            account.account_id,
            account.cash,
            [
                {
                    "symbol": item.symbol,
                    "quantity": item.quantity,
                    "sellable_quantity": (
                        item.sellable_quantity
                        if item.sellable_quantity is not None
                        else item.quantity
                    ),
                    "average_cost": item.average_cost,
                }
                for item in positions
            ],
        )
        daily = store.update_live_daily_snapshot(
            account.account_id, today, account.equity
        )
        if daily["daily_pnl"] <= -settings.max_daily_loss:
            reasons.append(
                "当日亏损 {:.2f} 已达到限额 {:.2f}".format(
                    daily["daily_pnl"], settings.max_daily_loss
                )
            )

        if side == "buy" and account.equity > 0:
            projected = []
            for item in positions:
                item_quote = quote_service.get(item.symbol)
                price = (
                    item_quote.price
                    if item_quote.fresh and item_quote.price
                    else item.average_cost
                )
                quantity_after = item.quantity + (
                    quantity if item.symbol == intent["symbol"] else 0
                )
                weight = quantity_after * price / account.equity
                if weight > 1:
                    reasons.append(
                        "{} 预计仓位 {:.1%} 超过账户净值".format(
                            item.symbol, weight
                        )
                    )
                projected.append(
                    PositionInput(
                        symbol=item.symbol,
                        weight=min(weight, 1.0),
                    )
                )
            if position is None:
                new_weight = notional / account.equity
                if new_weight > 1:
                    reasons.append(
                        "{} 预计仓位 {:.1%} 超过账户净值".format(
                            intent["symbol"], new_weight
                        )
                    )
                projected.append(
                    PositionInput(
                        symbol=intent["symbol"],
                        weight=min(new_weight, 1.0),
                    )
                )
            risk = risk_service.evaluate(
                RiskCheckRequest(
                    positions=projected,
                    proposed_turnover=notional / account.equity,
                    current_drawdown=max(
                        0.0,
                        1
                        - account.equity
                        / max(
                            account.equity - daily["daily_pnl"],
                            1.0,
                        ),
                    ),
                    order_notional_weight=notional / account.equity,
                )
            )
            reasons.extend(item["message"] for item in risk["breaches"])

        if reasons:
            raise PermissionError("实盘前置检查失败：" + "；".join(dict.fromkeys(reasons)))
        return {
            "approved": True,
            "notional": round(notional, 2),
            "account_id": account.account_id,
            "daily_pnl": round(daily["daily_pnl"], 2),
        }


pretrade_risk_service = PreTradeRiskService()
