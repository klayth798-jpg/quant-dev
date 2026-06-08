import math
from typing import Any, Dict, List, Optional, Tuple

from quantdev.analytics import mean, sample_std, spearman
from quantdev.store import store


class FactorService:
    minimum_observations = 21

    def _series_by_symbol(
        self, snapshot_id: str
    ) -> Dict[str, List[Dict[str, Any]]]:
        result: Dict[str, List[Dict[str, Any]]] = {}
        for row in store.list_prices(snapshot_id=snapshot_id):
            result.setdefault(row["symbol"], []).append(row)
        return result

    def _factor_value(
        self, factor_id: str, rows: List[Dict[str, Any]], index: int
    ) -> Optional[float]:
        if factor_id == "momentum_20":
            if index < 20:
                return None
            return rows[index]["close"] / rows[index - 20]["close"] - 1
        if factor_id == "reversal_5":
            if index < 5:
                return None
            return -(rows[index]["close"] / rows[index - 5]["close"] - 1)
        if factor_id == "low_volatility_20":
            if index < 20:
                return None
            daily_returns = [
                rows[cursor]["close"] / rows[cursor - 1]["close"] - 1
                for cursor in range(index - 19, index + 1)
            ]
            return -sample_std(daily_returns)
        if factor_id == "volume_ratio_20":
            if index < 19:
                return None
            short_volume = mean([row["volume"] for row in rows[index - 4 : index + 1]])
            long_volume = mean([row["volume"] for row in rows[index - 19 : index + 1]])
            return short_volume / long_volume - 1 if long_volume else None
        raise ValueError("未知因子: {}".format(factor_id))

    def build_panel(
        self, factor_id: str, snapshot_id: str
    ) -> Dict[str, Dict[str, float]]:
        if not store.get_factor(factor_id):
            raise ValueError("因子不存在: {}".format(factor_id))
        panel: Dict[str, Dict[str, float]] = {}
        for symbol, rows in self._series_by_symbol(snapshot_id).items():
            for index, row in enumerate(rows):
                value = self._factor_value(factor_id, rows, index)
                if value is not None and math.isfinite(value):
                    panel.setdefault(row["trade_date"], {})[symbol] = value
        return panel

    def evaluate(self, factor_id: str, forward_days: int = 5) -> Dict[str, Any]:
        snapshot_id = store.latest_snapshot_id()
        if not snapshot_id:
            raise ValueError("没有可用的数据快照")
        panel = self.build_panel(factor_id, snapshot_id)
        series = self._series_by_symbol(snapshot_id)
        forward_returns: Dict[Tuple[str, str], float] = {}
        for symbol, rows in series.items():
            for index, row in enumerate(rows[:-forward_days]):
                forward_returns[(row["trade_date"], symbol)] = (
                    rows[index + forward_days]["close"] / row["close"] - 1
                )

        daily_ic: List[float] = []
        long_short_returns: List[float] = []
        observations = 0
        for trade_date in sorted(panel):
            pairs = [
                (value, forward_returns[(trade_date, symbol)])
                for symbol, value in panel[trade_date].items()
                if (trade_date, symbol) in forward_returns
            ]
            if len(pairs) < 4:
                continue
            factors = [pair[0] for pair in pairs]
            returns = [pair[1] for pair in pairs]
            daily_ic.append(spearman(factors, returns))
            ordered = sorted(pairs, key=lambda pair: pair[0])
            bucket_size = max(1, len(ordered) // 3)
            bottom = mean([pair[1] for pair in ordered[:bucket_size]])
            top = mean([pair[1] for pair in ordered[-bucket_size:]])
            long_short_returns.append(top - bottom)
            observations += len(pairs)

        ic_mean = mean(daily_ic)
        ic_std = sample_std(daily_ic)
        periodicity = 252 / forward_days
        annualized_long_short = mean(long_short_returns) * periodicity
        metrics = {
            "ic_mean": round(ic_mean, 6),
            "ic_std": round(ic_std, 6),
            "ic_ir": round(ic_mean / ic_std * math.sqrt(periodicity), 4) if ic_std else 0.0,
            "positive_ic_ratio": round(
                sum(1 for value in daily_ic if value > 0) / len(daily_ic), 4
            )
            if daily_ic
            else 0.0,
            "annualized_long_short_return": round(annualized_long_short, 6),
            "observations": observations,
            "trading_days": len(daily_ic),
            "forward_days": forward_days,
        }
        return store.save_factor_run(
            factor_id=factor_id,
            snapshot_id=snapshot_id,
            parameters={"forward_days": forward_days},
            metrics=metrics,
        )


factor_service = FactorService()

