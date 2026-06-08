from typing import Any, Dict, List

from quantdev.analytics import summarize_equity
from quantdev.models import BacktestRequest
from quantdev.services.factors import factor_service
from quantdev.store import new_id, store, utc_now


class BacktestService:
    lot_size = 100
    minimum_commission = 5.0

    @staticmethod
    def _execution_price(close: float, side: str, slippage_bps: float) -> float:
        direction = 1 if side == "buy" else -1
        return close * (1 + direction * slippage_bps / 10_000)

    def run(self, request: BacktestRequest) -> Dict[str, Any]:
        snapshot_id = store.latest_snapshot_id()
        if not snapshot_id:
            raise ValueError("没有可用的数据快照")
        price_rows = store.list_prices(snapshot_id=snapshot_id)
        prices_by_date: Dict[str, Dict[str, float]] = {}
        for row in price_rows:
            prices_by_date.setdefault(row["trade_date"], {})[row["symbol"]] = row["close"]
        dates = sorted(prices_by_date)
        panel = factor_service.build_panel(request.factor_id, snapshot_id)

        cash = request.initial_capital
        positions: Dict[str, int] = {}
        equity_curve: List[Dict[str, Any]] = []
        trades: List[Dict[str, Any]] = []
        total_turnover = 0.0
        first_factor_date = min(panel) if panel else ""
        eligible_dates = [trade_date for trade_date in dates if trade_date >= first_factor_date]

        for day_index, trade_date in enumerate(eligible_dates):
            close_map = prices_by_date[trade_date]

            if day_index % request.rebalance_days == 0 and trade_date in panel:
                ranked = sorted(
                    panel[trade_date].items(), key=lambda item: item[1], reverse=True
                )
                selected = [symbol for symbol, _ in ranked[: request.top_n]]
                target_weight = 1 / len(selected) if selected else 0.0

                for symbol in list(positions):
                    if symbol in selected or symbol not in close_map:
                        continue
                    shares = positions.pop(symbol)
                    execution_price = self._execution_price(
                        close_map[symbol], "sell", request.slippage_bps
                    )
                    gross = shares * execution_price
                    commission = max(self.minimum_commission, gross * request.commission_rate)
                    tax = gross * request.stamp_duty_rate
                    cash += gross - commission - tax
                    total_turnover += gross
                    trades.append(
                        {
                            "date": trade_date,
                            "symbol": symbol,
                            "side": "sell",
                            "shares": shares,
                            "price": round(execution_price, 4),
                            "fees": round(commission + tax, 2),
                            "reason": "rebalance_exit",
                        }
                    )

                equity_after_sells = cash + sum(
                    shares * close_map.get(symbol, 0.0)
                    for symbol, shares in positions.items()
                )
                for symbol in selected:
                    if symbol not in close_map:
                        continue
                    target_value = equity_after_sells * target_weight
                    current_shares = positions.get(symbol, 0)
                    execution_price = self._execution_price(
                        close_map[symbol], "buy", request.slippage_bps
                    )
                    target_shares = (
                        int(target_value / execution_price / self.lot_size) * self.lot_size
                    )
                    shares_to_buy = max(0, target_shares - current_shares)
                    if shares_to_buy == 0:
                        continue
                    gross = shares_to_buy * execution_price
                    commission = max(self.minimum_commission, gross * request.commission_rate)
                    while shares_to_buy > 0 and gross + commission > cash:
                        shares_to_buy -= self.lot_size
                        gross = shares_to_buy * execution_price
                        commission = (
                            max(self.minimum_commission, gross * request.commission_rate)
                            if shares_to_buy
                            else 0.0
                        )
                    if shares_to_buy <= 0:
                        continue
                    cash -= gross + commission
                    positions[symbol] = current_shares + shares_to_buy
                    total_turnover += gross
                    trades.append(
                        {
                            "date": trade_date,
                            "symbol": symbol,
                            "side": "buy",
                            "shares": shares_to_buy,
                            "price": round(execution_price, 4),
                            "fees": round(commission, 2),
                            "reason": "factor_top_rank",
                        }
                    )

            equity = cash + sum(
                shares * close_map.get(symbol, 0.0) for symbol, shares in positions.items()
            )
            equity_curve.append(
                {
                    "date": trade_date,
                    "equity": round(equity, 2),
                    "cash": round(cash, 2),
                    "positions": len(positions),
                }
            )

        equity_values = [point["equity"] for point in equity_curve]
        metrics = summarize_equity(equity_values)
        metrics.update(
            {
                "trade_count": len(trades),
                "turnover_ratio": round(
                    total_turnover / request.initial_capital, 4
                ),
                "final_equity": round(equity_values[-1], 2) if equity_values else cash,
                "data_points": len(equity_curve),
            }
        )
        payload = {
            "run_id": new_id("bt"),
            "name": request.name,
            "factor_id": request.factor_id,
            "snapshot_id": snapshot_id,
            "config": request.model_dump(),
            "metrics": metrics,
            "equity_curve": equity_curve,
            "trades": trades,
            "status": "completed",
            "created_at": utc_now(),
        }
        store.save_backtest(payload)
        store.audit(
            actor="local-user",
            action="run_backtest",
            resource_type="backtest",
            resource_id=payload["run_id"],
            payload={"factor_id": request.factor_id, "snapshot_id": snapshot_id},
        )
        return payload


backtest_service = BacktestService()
