from bisect import bisect_right
from typing import Any, Dict, List, Optional

from quantdev.analytics import excess_metrics, summarize_equity
from quantdev.models import BacktestRequest
from quantdev.money import round_money, to_decimal
from quantdev.services.factors import factor_service
from quantdev.store import new_id, store, utc_now


class BacktestService:
    lot_size = 100
    minimum_commission = 5.0

    @staticmethod
    def _execution_price(close: float, side: str, slippage_bps: float) -> float:
        direction = 1 if side == "buy" else -1
        return close * (1 + direction * slippage_bps / 10_000)

    @staticmethod
    def _build_universe(
        index_codes: List[str],
    ) -> Optional[Dict[str, Any]]:
        """构建按时间点生效的可投资股票池。

        指数成分按月度快照存储，回测某一天的可投资标的取该日期之前
        （含当日）最近一次成分快照的成员，避免使用未来成分（前视偏差）。
        """
        rows = store.list_index_constituents(index_codes)
        if not rows:
            return None
        membership: Dict[str, Dict[str, set]] = {}
        for row in rows:
            membership.setdefault(row["index_code"], {}).setdefault(
                row["trade_date"], set()
            ).add(row["symbol"])
        snapshot_dates = {
            index_code: sorted(date_membership)
            for index_code, date_membership in membership.items()
        }
        if any(index_code not in membership for index_code in index_codes):
            return None
        return {
            "index_codes": index_codes,
            "dates": snapshot_dates,
            "membership": membership,
        }

    @staticmethod
    def _universe_at(universe: Dict[str, Any], trade_date: str) -> Optional[set]:
        allowed = set()
        for index_code in universe["index_codes"]:
            dates = universe["dates"][index_code]
            position = bisect_right(dates, trade_date) - 1
            if position < 0:
                return None
            snapshot_date = dates[position]
            allowed.update(universe["membership"][index_code][snapshot_date])
        return allowed

    @staticmethod
    def _st_symbols() -> set:
        """返回名称含 ST 的证券集合（含 *ST、SST 等风险警示股）。"""
        return {
            row["symbol"]
            for row in store.list_instruments()
            if "ST" in str(row.get("name", "")).upper()
        }

    @staticmethod
    def _price_limit_ratio(symbol: str, is_st: bool) -> float:
        """按板块与风险警示状态返回每日涨跌停幅度。"""
        if is_st:
            return 0.05
        if symbol.startswith(("300", "301")) or symbol.startswith(("688", "689")):
            return 0.20  # 创业板 / 科创板
        if symbol.startswith(("8", "4")) and symbol.endswith(".BJ"):
            return 0.30  # 北交所
        return 0.10  # 主板

    @staticmethod
    def _benchmark_curve(
        eligible_dates: List[str],
        prices_by_date: Dict[str, Dict[str, float]],
        universe: Optional[Dict[str, Any]],
        initial_capital: float,
    ) -> List[float]:
        """构建等权买入持有基准净值。

        每个交易日以"持有股票池(或全市场)全部成分、按日收益等权平均"近似
        一个等权基准组合，成分按时间点(point-in-time)取用，避免前视偏差。
        基准与策略共用同一组日期，便于逐日对齐计算超额收益。
        """
        equity = initial_capital
        curve = [equity]
        last_prices = dict(prices_by_date[eligible_dates[0]]) if eligible_dates else {}
        for index in range(1, len(eligible_dates)):
            prev_date = eligible_dates[index - 1]
            curr_date = eligible_dates[index]
            curr_close = prices_by_date[curr_date]
            if universe is not None:
                allowed = BacktestService._universe_at(universe, prev_date)
                symbols = [symbol for symbol in last_prices if allowed and symbol in allowed]
            else:
                symbols = list(last_prices)
            day_returns = [
                curr_close.get(symbol, last_prices[symbol]) / last_prices[symbol] - 1
                for symbol in symbols
                if last_prices[symbol]
            ]
            avg_return = sum(day_returns) / len(day_returns) if day_returns else 0.0
            equity *= 1 + avg_return
            curve.append(equity)
            last_prices.update(curr_close)
        return curve

    def _limit_state(
        self,
        symbol: str,
        trade_date: str,
        raw_close_map: Dict[str, float],
        previous_close: Dict[tuple, float],
        st_symbols: set,
        enabled: bool,
    ) -> str:
        if not enabled:
            return ""
        base = previous_close.get((trade_date, symbol))
        close = raw_close_map.get(symbol)
        if base is None or close is None:
            return ""
        ratio = self._price_limit_ratio(symbol, symbol in st_symbols)
        if close >= base * (1 + ratio) - 1e-6:
            return "up"
        if close <= base * (1 - ratio) + 1e-6:
            return "down"
        return ""

    def run(
        self,
        request: BacktestRequest,
        run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if run_id:
            existing = store.get_backtest(run_id)
            if existing:
                return existing
        snapshot_id = store.latest_snapshot_id()
        if not snapshot_id:
            raise ValueError("没有可用的数据快照")
        price_rows = store.list_prices(snapshot_id=snapshot_id)
        series = factor_service.group_price_rows(price_rows)
        panel = factor_service.build_panel(
            request.factor_id,
            snapshot_id,
            neutralize=request.neutralize,
            series=series,
        )

        latest_adj_factor = {
            symbol: float(rows[-1].get("adj_factor") or 1.0)
            for symbol, rows in series.items()
        }
        prices_by_date: Dict[str, Dict[str, float]] = {}
        raw_prices_by_date: Dict[str, Dict[str, float]] = {}
        for row in price_rows:
            symbol = row["symbol"]
            trade_date = row["trade_date"]
            raw_close = float(row["close"])
            adj_factor = float(row.get("adj_factor") or 1.0)
            base_factor = latest_adj_factor.get(symbol) or 1.0
            prices_by_date.setdefault(trade_date, {})[symbol] = (
                raw_close * adj_factor / base_factor
            )
            if request.apply_price_limit:
                raw_prices_by_date.setdefault(trade_date, {})[symbol] = raw_close
        dates = sorted(prices_by_date)

        st_symbols = self._st_symbols() if request.exclude_st else set()

        # 每个标的上一交易日收盘价，用于推算当日涨跌停价。
        prev_close: Dict[tuple, float] = {}
        if request.apply_price_limit:
            raw_series: Dict[str, List[tuple]] = {}
            for row in price_rows:
                raw_series.setdefault(row["symbol"], []).append(
                    (row["trade_date"], row["close"])
                )
            for symbol, rows in raw_series.items():
                rows.sort()
                for index in range(1, len(rows)):
                    prev_close[(rows[index][0], symbol)] = rows[index - 1][1]
        del price_rows
        del series

        universe = (
            self._build_universe(request.universe_indices)
            if request.universe_indices
            else None
        )
        if request.universe_indices and universe is None:
            raise ValueError(
                "所选指数没有可用的历史成分数据，请先同步指数成分或更换股票池"
            )

        cash = request.initial_capital
        positions: Dict[str, int] = {}
        equity_curve: List[Dict[str, Any]] = []
        trades: List[Dict[str, Any]] = []
        total_turnover = 0.0
        # T 日收盘后生成的目标持仓，在下一交易日(T+1)成交，避免前视偏差。
        pending_selection: Optional[List[str]] = None
        first_factor_date = min(panel) if panel else ""
        eligible_dates = [trade_date for trade_date in dates if trade_date >= first_factor_date]
        last_prices: Dict[str, float] = {}

        for day_index, trade_date in enumerate(eligible_dates):
            close_map = prices_by_date[trade_date]
            raw_close_map = raw_prices_by_date.get(trade_date, {})
            last_prices.update(close_map)

            # 执行上一信号日(T)生成的目标持仓：在当日(T+1)收盘成交，消除前视偏差。
            if pending_selection is not None:
                selected = pending_selection
                pending_selection = None
                target_weight = 1 / len(selected) if selected else 0.0
                portfolio_equity = cash + sum(
                    shares * last_prices.get(symbol, 0.0)
                    for symbol, shares in positions.items()
                )
                target_value = portfolio_equity * target_weight

                for symbol in list(positions):
                    if symbol not in close_map:
                        continue
                    desired_shares = (
                        int(target_value / close_map[symbol] / self.lot_size)
                        * self.lot_size
                        if symbol in selected and close_map[symbol] > 0
                        else 0
                    )
                    shares = max(0, positions[symbol] - desired_shares)
                    if shares == 0:
                        continue
                    if (
                        self._limit_state(
                            symbol,
                            trade_date,
                            raw_close_map,
                            prev_close,
                            st_symbols,
                            request.apply_price_limit,
                        )
                        == "down"
                    ):
                        continue  # 跌停无法卖出，继续持有
                    execution_price = self._execution_price(
                        close_map[symbol], "sell", request.slippage_bps
                    )
                    gross = round_money(shares * execution_price)
                    commission = round_money(
                        max(
                            to_decimal(self.minimum_commission),
                            to_decimal(gross) * to_decimal(request.commission_rate),
                        )
                    )
                    tax = round_money(to_decimal(gross) * to_decimal(request.stamp_duty_rate))
                    cash = round_money(
                        to_decimal(cash)
                        + to_decimal(gross)
                        - to_decimal(commission)
                        - to_decimal(tax)
                    )
                    remaining = positions[symbol] - shares
                    if remaining:
                        positions[symbol] = remaining
                    else:
                        positions.pop(symbol)
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

                for symbol in selected:
                    if symbol not in close_map:
                        continue
                    if (
                        self._limit_state(
                            symbol,
                            trade_date,
                            raw_close_map,
                            prev_close,
                            st_symbols,
                            request.apply_price_limit,
                        )
                        == "up"
                    ):
                        continue  # 涨停无法买入
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
                    gross = round_money(shares_to_buy * execution_price)
                    commission = round_money(
                        max(
                            to_decimal(self.minimum_commission),
                            to_decimal(gross) * to_decimal(request.commission_rate),
                        )
                    )
                    while shares_to_buy > 0 and gross + commission > cash:
                        shares_to_buy -= self.lot_size
                        gross = round_money(shares_to_buy * execution_price)
                        commission = (
                            round_money(
                                max(
                                    to_decimal(self.minimum_commission),
                                    to_decimal(gross) * to_decimal(request.commission_rate),
                                )
                            )
                            if shares_to_buy
                            else 0.0
                        )
                    if shares_to_buy <= 0:
                        continue
                    cash = round_money(
                        to_decimal(cash) - to_decimal(gross) - to_decimal(commission)
                    )
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

            # 信号日(T)收盘后基于当日因子生成目标持仓，留待下一交易日(T+1)成交。
            if day_index % request.rebalance_days == 0 and trade_date in panel:
                candidates = list(panel[trade_date].items())
                if universe is not None:
                    allowed = self._universe_at(universe, trade_date)
                    candidates = (
                        [
                            (symbol, value)
                            for symbol, value in candidates
                            if symbol in allowed
                        ]
                        if allowed is not None
                        else []
                    )
                if st_symbols:
                    candidates = [
                        (symbol, value)
                        for symbol, value in candidates
                        if symbol not in st_symbols
                    ]
                ranked = sorted(
                    candidates, key=lambda item: item[1], reverse=True
                )
                ranked_symbols = [symbol for symbol, _ in ranked]
                if request.buffer_multiple > 1.0:
                    # 持仓缓冲带：已持有标的只要仍排在 top_n×buffer 之内就保留，
                    # 再用排名靠前的新标的补足到 top_n，降低因排名小幅波动
                    # 导致的频繁调仓。
                    buffer_size = int(request.top_n * request.buffer_multiple)
                    buffer_symbols = set(ranked_symbols[:buffer_size])
                    kept = [s for s in positions if s in buffer_symbols]
                    selection = list(kept[: request.top_n])
                    for symbol in ranked_symbols:
                        if len(selection) >= request.top_n:
                            break
                        if symbol not in selection:
                            selection.append(symbol)
                else:
                    selection = list(ranked_symbols[: request.top_n])
                pending_selection = selection

            equity = cash + sum(
                shares * last_prices.get(symbol, 0.0)
                for symbol, shares in positions.items()
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
        benchmark_curve = self._benchmark_curve(
            eligible_dates, prices_by_date, universe, request.initial_capital
        )
        if len(benchmark_curve) == len(equity_values):
            metrics.update(excess_metrics(equity_values, benchmark_curve))
            for point, benchmark_value in zip(equity_curve, benchmark_curve):
                point["benchmark"] = round(benchmark_value, 2)
        payload = {
            "run_id": run_id or new_id("bt"),
            "name": request.name,
            "factor_id": request.factor_id,
            "snapshot_id": snapshot_id,
            "manifest_id": (
                store.latest_dataset_manifest(snapshot_id) or {}
            ).get("manifest_id"),
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
