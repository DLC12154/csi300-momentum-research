"""Point-in-time universe, retrospective holdout, and executable backtest.

Research definitions are intentionally separated from the original notebook:
signals are observed at close t, orders execute at open t+1, and only a long
Q5 portfolio is treated as executable in the A-share market.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats


ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"
PROCESSED_DIR = ROOT / "data" / "processed" / "advanced"
FIGURE_DIR = ROOT / "figures"

LOOKBACKS = (5, 10, 20, 60)
PRIMARY_LOOKBACK = 20
HOLDING_DAYS = 5
MIN_HISTORY = 120
MIN_ADV20_CNY = 30_000_000
DEVELOPMENT_START = pd.Timestamp("2020-01-02")
DEVELOPMENT_END = pd.Timestamp("2023-12-29")
HOLDOUT_START = pd.Timestamp("2024-01-02")
HOLDOUT_END = pd.Timestamp("2025-12-31")

INITIAL_CAPITAL = 100_000_000.0
COMMISSION_RATE = 0.0003
MIN_COMMISSION = 5.0
SLIPPAGE_RATE = 0.0005
MAX_ADV_PARTICIPATION = 0.05
MIN_ORDER_NOTIONAL = 1_000.0


@dataclass(frozen=True)
class CostModel:
    commission_rate: float
    minimum_commission: float
    slippage_rate: float
    stamp_duty_pre_2023: float = 0.001
    stamp_duty_post_2023: float = 0.0005
    stamp_duty_changed: pd.Timestamp = pd.Timestamp("2023-08-28")

    def stamp_duty(self, date: pd.Timestamp) -> float:
        return (
            self.stamp_duty_post_2023
            if date >= self.stamp_duty_changed
            else self.stamp_duty_pre_2023
        )


REALISTIC_COSTS = CostModel(
    commission_rate=COMMISSION_RATE,
    minimum_commission=MIN_COMMISSION,
    slippage_rate=SLIPPAGE_RATE,
)
ZERO_COSTS = CostModel(
    commission_rate=0.0,
    minimum_commission=0.0,
    slippage_rate=0.0,
    stamp_duty_pre_2023=0.0,
    stamp_duty_post_2023=0.0,
)


@dataclass
class MarketData:
    calendar: pd.DatetimeIndex
    codes: list[str]
    open: pd.DataFrame
    close: pd.DataFrame
    close_marked: pd.DataFrame
    preclose: pd.DataFrame
    amount: pd.DataFrame
    traded: pd.DataFrame
    can_buy: pd.DataFrame
    can_sell: pd.DataFrame
    limit_up: pd.DataFrame
    limit_down: pd.DataFrame
    is_st: pd.DataFrame


def load_inputs() -> tuple[pd.DataFrame, pd.DataFrame]:
    prices = pd.read_parquet(RAW_DIR / "csi300_prices_historical.parquet")
    membership = pd.read_csv(
        RAW_DIR / "csi300_historical_membership.csv",
        dtype={"code": str},
        parse_dates=["query_date", "index_effective_date"],
    )
    prices["date"] = pd.to_datetime(prices["date"])
    prices["code"] = prices["code"].astype(str).str.zfill(6)
    membership["code"] = membership["code"].astype(str).str.zfill(6)
    return prices, membership


def _dense_field(panel: pd.DataFrame, field: str, index: pd.MultiIndex):
    return (
        panel.set_index(["date", "code"])[field]
        .reindex(index)
        .unstack("code")
    )


def _limit_band(codes: list[str], calendar: pd.DatetimeIndex) -> pd.DataFrame:
    frame = pd.DataFrame(0.10, index=calendar, columns=codes)
    chi_next_start = pd.Timestamp("2020-08-24")
    for code in codes:
        if code.startswith(("688", "689")):
            frame.loc[:, code] = 0.20
        elif code.startswith(("300", "301")):
            frame.loc[calendar < chi_next_start, code] = 0.10
            frame.loc[calendar >= chi_next_start, code] = 0.20
    return frame


def build_market(panel: pd.DataFrame) -> MarketData:
    panel = panel.copy()
    panel["date"] = pd.to_datetime(panel["date"])
    panel["code"] = panel["code"].astype(str).str.zfill(6)
    calendar = pd.DatetimeIndex(sorted(panel["date"].unique()))
    codes = sorted(panel["code"].unique())
    index = pd.MultiIndex.from_product(
        [calendar, codes], names=["date", "code"]
    )
    open_price = _dense_field(panel, "open", index)
    close = _dense_field(panel, "close", index)
    preclose = _dense_field(panel, "preclose", index)
    volume = _dense_field(panel, "volume", index)
    amount = _dense_field(panel, "amount", index)
    trade_status = _dense_field(panel, "tradestatus", index)
    st_flag = _dense_field(panel, "isST", index).ffill()

    traded = (
        (trade_status == 1)
        & volume.gt(0)
        & open_price.notna()
        & close.notna()
    )
    is_st = st_flag.eq(1) | st_flag.eq("1")
    limit = _limit_band(codes, calendar)
    limit = limit.mask(is_st, 0.05)
    open_change = open_price.div(preclose).sub(1)
    tolerance = 0.002
    limit_up = traded & open_change.ge(limit - tolerance)
    limit_down = traded & open_change.le(-limit + tolerance)
    return MarketData(
        calendar=calendar,
        codes=codes,
        open=open_price,
        close=close,
        close_marked=close.ffill(),
        preclose=preclose,
        amount=amount,
        traded=traded,
        can_buy=traded & ~limit_up,
        can_sell=traded & ~limit_down,
        limit_up=limit_up,
        limit_down=limit_down,
        is_st=is_st,
    )


def build_signal_panel(
    market: MarketData, membership: pd.DataFrame
) -> pd.DataFrame:
    """Create one weekly cross-section on the same phase for all lookbacks."""
    close = market.close_marked
    moments = {
        lookback: close.div(close.shift(lookback)).sub(1)
        for lookback in LOOKBACKS
    }
    forward_open = (
        market.open.shift(-(HOLDING_DAYS + 1))
        .div(market.open.shift(-1))
        .sub(1)
    )
    adv20 = market.amount.where(market.traded).rolling(
        20, min_periods=15
    ).mean()
    active_history = market.traded.astype(float).rolling(
        MIN_HISTORY, min_periods=MIN_HISTORY
    ).sum().shift(1)
    entry_date = pd.Series(market.calendar[1:], index=market.calendar[:-1])
    exit_date = pd.Series(market.calendar[6:], index=market.calendar[:-6])

    membership_dates = sorted(membership["query_date"].unique())
    membership_map = (
        membership.groupby("query_date")["code"].agg(set).to_dict()
    )
    rows: list[dict] = []
    for date in membership_dates:
        date = pd.Timestamp(date)
        if date not in market.calendar:
            continue
        codes = sorted(membership_map[date])
        for code in codes:
            history = active_history.at[date, code]
            adv = adv20.at[date, code]
            if (
                not market.traded.at[date, code]
                or market.is_st.at[date, code]
                or not np.isfinite(history)
                or history < MIN_HISTORY
                or not np.isfinite(adv)
                or adv < MIN_ADV20_CNY
            ):
                continue
            row = {
                "signal_date": date,
                "execution_date": entry_date.get(date, pd.NaT),
                "exit_date": exit_date.get(date, pd.NaT),
                "code": code,
                "forward_open_5": forward_open.at[date, code],
                "adv20": adv,
                "active_history": history,
            }
            for lookback in LOOKBACKS:
                row[f"momentum_{lookback}"] = moments[lookback].at[date, code]
            if np.isfinite(row[f"momentum_{PRIMARY_LOOKBACK}"]):
                rows.append(row)

    signal = pd.DataFrame(rows)
    if signal.empty:
        raise ValueError("No valid signal rows were constructed")
    # The final signal may have no t+6 label inside the sample. It must still
    # enter the observable cross-section and executable portfolio target.
    # Future return availability is used only when evaluating a realized label.
    signal = signal.loc[signal["execution_date"].notna()].copy()
    for lookback in LOOKBACKS:
        column = f"momentum_{lookback}"
        group_column = f"group_{lookback}"
        signal[group_column] = np.nan
        valid = signal[column].notna()
        signal.loc[valid, group_column] = (
            signal.loc[valid]
            .groupby("signal_date")[column]
            .transform(
                lambda values: pd.qcut(
                    values.rank(method="first"),
                    q=5,
                    labels=[1, 2, 3, 4, 5],
                ).astype(float)
            )
        )
    signal["stage"] = np.where(
        signal["signal_date"] <= DEVELOPMENT_END,
        "development_2020_2023",
        "retrospective_holdout_2024_2025",
    )
    return signal.sort_values(["signal_date", "code"]).reset_index(drop=True)


def data_quality_report(
    prices: pd.DataFrame, membership: pd.DataFrame, signal: pd.DataFrame
) -> dict:
    snapshot_counts = membership.groupby("query_date")["code"].nunique()
    current = pd.read_csv(
        RAW_DIR / "csi300_constituents.csv",
        dtype={"\u6210\u5206\u5238\u4ee3\u7801": str},
    )
    current_codes = set(
        current["\u6210\u5206\u5238\u4ee3\u7801"].astype(str).str.zfill(6)
    )
    historical_codes = set(membership["code"])
    membership_prices = pd.MultiIndex.from_frame(
        membership[["query_date", "code"]]
    )
    price_keys = pd.MultiIndex.from_frame(prices[["date", "code"]])
    missing_membership_prices = int((~membership_prices.isin(price_keys)).sum())
    execution_lag = (
        signal["execution_date"] - signal["signal_date"]
    ).dt.days.value_counts().sort_index()
    quality = {
        "membership_snapshots": int(membership["query_date"].nunique()),
        "membership_rows": int(len(membership)),
        "all_membership_snapshots_have_300_codes": bool(
            (snapshot_counts == 300).all()
        ),
        "historical_union_codes": len(historical_codes),
        "current_constituent_codes": len(current_codes),
        "historical_codes_no_longer_current": len(
            historical_codes - current_codes
        ),
        "current_codes_outside_research_window": len(
            current_codes - historical_codes
        ),
        "price_rows": int(len(prices)),
        "price_codes": int(prices["code"].nunique()),
        "price_date_start": str(prices["date"].min().date()),
        "price_date_end": str(prices["date"].max().date()),
        "duplicate_price_keys": int(
            prices.duplicated(["date", "code"]).sum()
        ),
        "null_ohlc_fields": {
            column: int(prices[column].isna().sum())
            for column in ["open", "high", "low", "close", "preclose"]
        },
        "membership_rows_without_same_day_price": missing_membership_prices,
        "signal_dates": int(signal["signal_date"].nunique()),
        "signal_rows": int(len(signal)),
        "duplicate_signal_keys": int(
            signal.duplicated(["signal_date", "code"]).sum()
        ),
        "signal_rows_per_date": {
            "minimum": int(signal.groupby("signal_date").size().min()),
            "median": float(signal.groupby("signal_date").size().median()),
            "maximum": int(signal.groupby("signal_date").size().max()),
        },
        "minimum_active_history_days": float(signal["active_history"].min()),
        "minimum_adv20_cny": float(signal["adv20"].min()),
        "execution_lag_calendar_days": {
            str(int(days)): int(count)
            for days, count in execution_lag.items()
        },
        "rows_without_realized_exit_label": int(
            signal["forward_open_5"].isna().sum()
        ),
        "all_available_exits_after_execution": bool(
            (
                signal.dropna(subset=["exit_date"])["exit_date"]
                > signal.dropna(subset=["exit_date"])["execution_date"]
            ).all()
        ),
        "stage_signal_observations": {
            stage: int(frame["signal_date"].nunique())
            for stage, frame in signal.groupby("stage")
        },
    }
    assert quality["all_membership_snapshots_have_300_codes"]
    assert quality["duplicate_price_keys"] == 0
    assert quality["duplicate_signal_keys"] == 0
    assert quality["all_available_exits_after_execution"]
    assert missing_membership_prices <= 5
    assert all(value == 0 for value in quality["null_ohlc_fields"].values())
    return quality


def _one_sample_stats(values: pd.Series) -> tuple[float, float]:
    clean = values.replace([np.inf, -np.inf], np.nan).dropna()
    if len(clean) < 2 or clean.std(ddof=1) == 0:
        return np.nan, np.nan
    result = stats.ttest_1samp(clean, popmean=0.0)
    return float(result.statistic), float(result.pvalue)


def analyze_factors(
    signal: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    stage_rank_ic = []
    group_returns = []
    summary = []
    for stage, stage_frame in signal.groupby("stage", sort=False):
        for lookback in LOOKBACKS:
            column = f"momentum_{lookback}"
            group_column = f"group_{lookback}"
            valid = stage_frame.dropna(
                subset=[column, "forward_open_5", group_column]
            )
            rank_ic = (
                valid.groupby("signal_date")
                .apply(
                    lambda frame: frame[column].corr(
                        frame["forward_open_5"], method="spearman"
                    ),
                    include_groups=False,
                )
                .rename("rank_ic")
            )
            rank_ic_frame = rank_ic.rename_axis("signal_date").reset_index()
            rank_ic_frame["lookback"] = lookback
            rank_ic_frame["stage"] = stage
            stage_rank_ic.append(rank_ic_frame)

            by_group = (
                valid.groupby([group_column, "signal_date"])["forward_open_5"]
                .mean()
                .groupby(level=0)
                .mean()
            )
            for group, value in by_group.items():
                group_returns.append(
                    {
                        "stage": stage,
                        "lookback": lookback,
                        "group": int(group),
                        "mean_forward_open_5": value,
                    }
                )
            daily_group = valid.pivot_table(
                index="signal_date",
                columns=group_column,
                values="forward_open_5",
                aggfunc="mean",
            )
            long_short = daily_group.get(5.0, pd.Series(dtype=float)) - (
                daily_group.get(1.0, pd.Series(dtype=float))
            )
            t_stat, p_value = _one_sample_stats(rank_ic)
            summary.append(
                {
                    "stage": stage,
                    "lookback": lookback,
                    "ic_observations": int(rank_ic.notna().sum()),
                    "mean_rank_ic": rank_ic.mean(),
                    "rank_ic_std": rank_ic.std(ddof=1),
                    "rank_icir": rank_ic.mean() / rank_ic.std(ddof=1),
                    "positive_ratio": rank_ic.gt(0).mean(),
                    "rank_ic_t_stat": t_stat,
                    "rank_ic_p_value": p_value,
                    "mean_q5_q1": long_short.mean(),
                }
            )
    rank_ic_table = pd.concat(stage_rank_ic, ignore_index=True)
    group_table = pd.DataFrame(group_returns)
    summary_table = pd.DataFrame(summary)
    return rank_ic_table, group_table, summary_table


def _trade_commission(notional: float, costs: CostModel) -> float:
    if notional <= 0 or costs.commission_rate <= 0:
        return 0.0
    return max(costs.minimum_commission, notional * costs.commission_rate)


def _maximum_affordable_notional(cash: float, costs: CostModel) -> float:
    """Solve for notional including proportional/minimum fees and slippage."""
    if cash <= 0:
        return 0.0
    low = 0.0
    high = cash / (1 + costs.slippage_rate)
    for _ in range(40):
        middle = (low + high) / 2
        all_in = (
            middle * (1 + costs.slippage_rate)
            + _trade_commission(middle, costs)
        )
        if all_in <= cash:
            low = middle
        else:
            high = middle
    return low


def simulate_targets(
    market: MarketData,
    targets: pd.DataFrame,
    costs: CostModel,
    label: str,
) -> tuple[pd.Series, pd.DataFrame, pd.DataFrame]:
    """Simulate long-only target holdings with daily NAV.

    Quantities are units of a total-return synthetic portfolio (hfq prices),
    which avoids dropping dividend returns.  Every order is still constrained
    by the exchange trading state, open-price limit checks, and ADV capacity.
    """
    if targets.empty:
        raise ValueError("Target holdings are empty")
    targets = targets.copy()
    targets["execution_date"] = pd.to_datetime(targets["execution_date"])
    target_map = {
        date: frame.drop_duplicates("code").set_index("code")
        for date, frame in targets.groupby("execution_date")
    }
    first_date = min(target_map)
    sim_dates = market.calendar[market.calendar >= first_date]
    cash = INITIAL_CAPITAL
    holdings: dict[str, float] = {}
    nav_rows = []
    trade_rows = []
    rebalance_rows = []
    market_adv20 = market.amount.where(market.traded).rolling(
        20, min_periods=15
    ).mean()

    for date in sim_dates:
        current_target = target_map.get(date)
        if current_target is not None:
            target_codes = set(current_target.index)
            target_count = len(target_codes)
            open_row = market.open.loc[date]
            marked_row = market.close_marked.loc[date]
            prices = open_row.where(open_row.notna(), marked_row)
            pretrade_value = cash + sum(
                units * prices.get(code, np.nan)
                for code, units in holdings.items()
                if np.isfinite(prices.get(code, np.nan))
            )
            desired_value = pretrade_value / target_count
            desired_values = {code: desired_value for code in target_codes}
            gross_traded = 0.0
            total_cost = 0.0
            blocked_orders = 0

            # Sell first. Orders that cannot cross a suspension or limit-down
            # remain in the portfolio and are retried at the next rebalance.
            for code in sorted(set(holdings) | target_codes):
                units = holdings.get(code, 0.0)
                price = prices.get(code, np.nan)
                if units <= 0 or not np.isfinite(price):
                    continue
                desired = desired_values.get(code, 0.0)
                reduction = max(0.0, units * price - desired)
                if reduction < MIN_ORDER_NOTIONAL:
                    continue
                adv = (
                    current_target.loc[code, "adv20"]
                    if code in current_target.index
                    else market_adv20.at[date, code]
                )
                capacity = (
                    MAX_ADV_PARTICIPATION * adv
                    if np.isfinite(adv)
                    else 0.0
                )
                order_notional = min(reduction, capacity)
                if order_notional < MIN_ORDER_NOTIONAL:
                    blocked_orders += 1
                    continue
                if not bool(market.can_sell.at[date, code]):
                    blocked_orders += 1
                    reason = (
                        "limit_down"
                        if bool(market.limit_down.at[date, code])
                        else "not_trading"
                    )
                    trade_rows.append(
                        {
                            "date": date,
                            "code": code,
                            "side": "sell",
                            "desired_notional": reduction,
                            "executed_notional": 0.0,
                            "cost": 0.0,
                            "status": reason,
                            "strategy": label,
                        }
                    )
                    continue
                quantity = order_notional / price
                commission = _trade_commission(order_notional, costs)
                stamp = order_notional * costs.stamp_duty(date)
                slippage = order_notional * costs.slippage_rate
                cash += order_notional - commission - stamp - slippage
                holdings[code] = units - quantity
                if holdings[code] * price < MIN_ORDER_NOTIONAL:
                    holdings.pop(code, None)
                gross_traded += order_notional
                total_cost += commission + stamp + slippage
                trade_rows.append(
                    {
                        "date": date,
                        "code": code,
                        "side": "sell",
                        "desired_notional": reduction,
                        "executed_notional": order_notional,
                        "cost": commission + stamp + slippage,
                        "status": (
                            "capacity_limited"
                            if capacity < reduction
                            else "filled"
                        ),
                        "strategy": label,
                    }
                )

            # Revalue at the same open after sells, then spend available cash.
            equity_after_sells = cash + sum(
                units * prices.get(code, np.nan)
                for code, units in holdings.items()
                if np.isfinite(prices.get(code, np.nan))
            )
            desired_value = equity_after_sells / target_count
            for code in sorted(target_codes):
                price = prices.get(code, np.nan)
                if not np.isfinite(price):
                    blocked_orders += 1
                    continue
                units = holdings.get(code, 0.0)
                gap = max(0.0, desired_value - units * price)
                if gap < MIN_ORDER_NOTIONAL:
                    continue
                max_cash_order = _maximum_affordable_notional(cash, costs)
                capacity = (
                    MAX_ADV_PARTICIPATION * current_target.loc[code, "adv20"]
                )
                order_notional = min(gap, max_cash_order, capacity)
                if order_notional < MIN_ORDER_NOTIONAL:
                    if capacity < MIN_ORDER_NOTIONAL:
                        blocked_orders += 1
                    continue
                if not bool(market.can_buy.at[date, code]):
                    blocked_orders += 1
                    reason = (
                        "limit_up"
                        if bool(market.limit_up.at[date, code])
                        else "not_trading"
                    )
                    trade_rows.append(
                        {
                            "date": date,
                            "code": code,
                            "side": "buy",
                            "desired_notional": gap,
                            "executed_notional": 0.0,
                            "cost": 0.0,
                            "status": reason,
                            "strategy": label,
                        }
                    )
                    continue
                commission = _trade_commission(order_notional, costs)
                slippage = order_notional * costs.slippage_rate
                all_in = order_notional + commission + slippage
                if all_in > cash + 0.01:
                    raise RuntimeError(
                        f"Buy exceeds cash on {date.date()}: "
                        f"{all_in:.2f} > {cash:.2f}"
                    )
                cash -= all_in
                holdings[code] = units + order_notional / price
                gross_traded += order_notional
                total_cost += commission + slippage
                trade_rows.append(
                    {
                        "date": date,
                        "code": code,
                        "side": "buy",
                        "desired_notional": gap,
                        "executed_notional": order_notional,
                        "cost": commission + slippage,
                        "status": (
                            "capacity_limited"
                            if capacity < gap or cash <= 0
                            else "filled"
                        ),
                        "strategy": label,
                    }
                )

            rebalance_rows.append(
                {
                    "date": date,
                    "strategy": label,
                    "target_count": target_count,
                    "held_count": len(holdings),
                    "one_way_turnover": (
                        gross_traded / (2 * pretrade_value)
                        if pretrade_value
                        else np.nan
                    ),
                    "trading_cost": total_cost,
                    "blocked_orders": blocked_orders,
                }
            )
            if cash < -0.01:
                raise RuntimeError(f"Negative cash after {date.date()}: {cash}")
            if any(units < 0 for units in holdings.values()):
                raise RuntimeError(f"Negative holding after {date.date()}")

        close_row = market.close_marked.loc[date]
        portfolio_value = cash + sum(
            units * close_row.get(code, np.nan)
            for code, units in holdings.items()
            if np.isfinite(close_row.get(code, np.nan))
        )
        if not np.isfinite(portfolio_value) or portfolio_value <= 0:
            raise RuntimeError(f"Invalid portfolio value on {date.date()}")
        nav_rows.append({"date": date, "nav": portfolio_value / INITIAL_CAPITAL})

    nav = (
        pd.DataFrame(nav_rows)
        .set_index("date")["nav"]
        .rename(label)
        .sort_index()
    )
    return nav, pd.DataFrame(trade_rows), pd.DataFrame(rebalance_rows)


def period_metrics(
    nav: pd.Series,
    start: pd.Timestamp,
    end: pd.Timestamp,
    initial: float = 1.0,
) -> dict:
    before = nav.loc[nav.index < start]
    base = float(before.iloc[-1]) if not before.empty else initial
    segment = nav.loc[(nav.index >= start) & (nav.index <= end)]
    if segment.empty:
        return {}
    values = pd.concat(
        [pd.Series([base], index=[start - pd.Timedelta(days=1)]), segment]
    )
    returns = values.pct_change().dropna()
    total_return = values.iloc[-1] / values.iloc[0] - 1
    periods = max(len(returns), 1)
    cagr = (1 + total_return) ** (252 / periods) - 1
    volatility = returns.std(ddof=1) * np.sqrt(252)
    sharpe = (
        returns.mean() / returns.std(ddof=1) * np.sqrt(252)
        if returns.std(ddof=1) > 0
        else np.nan
    )
    running_max = values.cummax()
    max_drawdown = (values / running_max - 1).min()
    return {
        "observations": len(segment),
        "total_return": total_return,
        "cagr": cagr,
        "annual_volatility": volatility,
        "sharpe": sharpe,
        "max_drawdown": max_drawdown,
        "ending_nav": values.iloc[-1],
    }


def build_performance_table(
    navs: dict[str, pd.Series],
    rebalances: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    rows = []
    periods = {
        "development_2020_2023": (DEVELOPMENT_START, DEVELOPMENT_END),
        "retrospective_holdout_2024_2025": (HOLDOUT_START, HOLDOUT_END),
        "full_2020_2025": (DEVELOPMENT_START, HOLDOUT_END),
    }
    for period, (start, end) in periods.items():
        for label, nav in navs.items():
            metrics = period_metrics(nav, start, end)
            rebalance = rebalances[label]
            in_period = rebalance[
                (rebalance["date"] >= start) & (rebalance["date"] <= end)
            ]
            rows.append(
                {
                    "period": period,
                    "portfolio": label,
                    **metrics,
                    "mean_one_way_turnover": in_period[
                        "one_way_turnover"
                    ].mean(),
                    "total_trading_cost_cny": in_period["trading_cost"].sum(),
                    "blocked_orders": in_period["blocked_orders"].sum(),
                }
            )
    performance = pd.DataFrame(rows)
    for period in periods:
        mask = performance["period"] == period
        benchmark = performance.loc[
            mask & (performance["portfolio"] == "benchmark_net"), "total_return"
        ]
        if not benchmark.empty:
            performance.loc[mask, "excess_return_vs_benchmark"] = (
                performance.loc[mask, "total_return"] - benchmark.iloc[0]
            )
    return performance


def make_figures(
    summary: pd.DataFrame,
    navs: dict[str, pd.Series],
    rebalances: dict[str, pd.DataFrame],
) -> None:
    FIGURE_DIR.mkdir(parents=True, exist_ok=True)
    stage_ic = summary.pivot(
        index="lookback", columns="stage", values="mean_rank_ic"
    )
    axis = stage_ic.plot(kind="bar", figsize=(8, 4.5))
    axis.axhline(0, color="black", linewidth=0.8)
    axis.set_title("Rank IC by point-in-time stage")
    axis.set_xlabel("Lookback (trading days)")
    axis.set_ylabel("Mean Rank IC")
    axis.figure.tight_layout()
    axis.figure.savefig(FIGURE_DIR / "04_stage_rank_ic.png", dpi=140)
    plt.close(axis.figure)

    holdout = pd.concat(
        [
            navs["q5_net"].rename("Q5 executable net"),
            navs["q5_gross"].rename("Q5 executable gross"),
            navs["benchmark_net"].rename("Dynamic CSI 300 net"),
        ],
        axis=1,
    ).ffill()
    holdout = holdout.loc[holdout.index >= HOLDOUT_START]
    holdout = holdout.div(holdout.iloc[0]).mul(10_000_000)
    axis = holdout.plot(figsize=(11, 5))
    axis.set_title("Retrospective time holdout portfolio value")
    axis.set_xlabel("")
    axis.set_ylabel("Portfolio value (CNY, start = 10m)")
    axis.figure.tight_layout()
    axis.figure.savefig(
        FIGURE_DIR / "04_retrospective_holdout_nav.png", dpi=140
    )
    plt.close(axis.figure)

    q5_rebalance = rebalances["q5_net"]
    plot_data = q5_rebalance.set_index("date")[
        ["one_way_turnover", "trading_cost"]
    ].rename(
        columns={
            "one_way_turnover": "One-way turnover",
            "trading_cost": "Trading cost (CNY)",
        }
    )
    plot_data["Trading cost (CNY)"] = plot_data["Trading cost (CNY)"]
    figure, left = plt.subplots(figsize=(11, 4.5))
    plot_data["One-way turnover"].plot(ax=left, color="#2b6cb0")
    left.set_xlabel("")
    left.set_ylabel("Turnover")
    right = left.twinx()
    plot_data["Trading cost (CNY)"].plot(ax=right, color="#c05621")
    right.set_ylabel("Cost (CNY)")
    left.set_title("Executable Q5 turnover and trading costs")
    figure.tight_layout()
    figure.savefig(FIGURE_DIR / "04_turnover_and_costs.png", dpi=140)
    plt.close(figure)


def write_outputs(
    script: Path,
    summary: pd.DataFrame,
    rank_ic: pd.DataFrame,
    groups: pd.DataFrame,
    signal: pd.DataFrame,
    performance: pd.DataFrame,
    navs: dict[str, pd.Series],
    trades: dict[str, pd.DataFrame],
    rebalances: dict[str, pd.DataFrame],
    quality: dict,
) -> None:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    summary.to_csv(PROCESSED_DIR / "rank_ic_by_stage.csv", index=False)
    rank_ic.to_csv(PROCESSED_DIR / "weekly_rank_ic.csv", index=False)
    groups.to_csv(PROCESSED_DIR / "group_returns_by_stage.csv", index=False)
    performance.to_csv(PROCESSED_DIR / "executable_performance.csv", index=False)
    signal.to_csv(
        PROCESSED_DIR / "eligible_signal_panel.csv.gz",
        index=False,
        compression="gzip",
    )
    nav_frame = pd.DataFrame(navs)
    nav_frame.to_csv(PROCESSED_DIR / "daily_nav.csv")
    pd.concat(trades.values(), ignore_index=True).to_csv(
        PROCESSED_DIR / "trade_log.csv.gz",
        index=False,
        compression="gzip",
    )
    pd.concat(rebalances.values(), ignore_index=True).to_csv(
        PROCESSED_DIR / "rebalance_log.csv", index=False
    )
    metadata = {
        "script": str(script.relative_to(ROOT)),
        "primary_lookback": PRIMARY_LOOKBACK,
        "holding_days": HOLDING_DAYS,
        "signal": "close_t",
        "execution": "open_t_plus_1",
        "long_only_primary": True,
        "short_leg": "diagnostic_only_not_assumed_executable",
        "development_period": [
            str(DEVELOPMENT_START.date()),
            str(DEVELOPMENT_END.date()),
        ],
        "retrospective_holdout_period": [
            str(HOLDOUT_START.date()),
            str(HOLDOUT_END.date()),
        ],
        "holdout_character": (
            "Retrospective time holdout. The original project and Git history "
            "already contained 2024-2025 results; this is not a pre-frozen OOS."
        ),
        "min_history_days": MIN_HISTORY,
        "min_adv20_cny": MIN_ADV20_CNY,
        "adv_participation_cap": MAX_ADV_PARTICIPATION,
        "initial_capital_cny": INITIAL_CAPITAL,
        "commission_rate": COMMISSION_RATE,
        "minimum_commission_cny": MIN_COMMISSION,
        "slippage_rate": SLIPPAGE_RATE,
        "stamp_duty_pre_2023_08_28": REALISTIC_COSTS.stamp_duty_pre_2023,
        "stamp_duty_post_2023_08_28": REALISTIC_COSTS.stamp_duty_post_2023,
        "limit_rules": {
            "main_board": "10%, ST 5%",
            "star_market": "20%, ST 5%",
            "chinext": "10% before 2020-08-24; 20% thereafter; ST 5%",
            "entry_block": "suspension or open at/near limit-up",
            "exit_block": "suspension or open at/near limit-down",
        },
        "known_omissions": [
            "board-lot rounding",
            "bid-ask depth and permanent market impact",
            "dividend tax and financing costs for the long-only sleeve",
        ],
    }
    (PROCESSED_DIR / "run_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (PROCESSED_DIR / "data_quality.json").write_text(
        json.dumps(quality, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> None:
    print("Loading historical universe and prices...", flush=True)
    prices, membership = load_inputs()
    market = build_market(prices)
    print("Building point-in-time weekly signals...", flush=True)
    signal = build_signal_panel(market, membership)
    quality = data_quality_report(prices, membership, signal)
    rank_ic, groups, summary = analyze_factors(signal)

    primary = signal[signal[f"group_{PRIMARY_LOOKBACK}"] == 5].copy()
    benchmark = signal[signal[f"momentum_{PRIMARY_LOOKBACK}"].notna()].copy()
    print(
        f"Simulating executable portfolios on {primary['execution_date'].nunique()} "
        "rebalance dates...",
        flush=True,
    )
    q5_net, trades_net, rebalance_net = simulate_targets(
        market, primary, REALISTIC_COSTS, "q5_net"
    )
    q5_gross, trades_gross, rebalance_gross = simulate_targets(
        market, primary, ZERO_COSTS, "q5_gross"
    )
    benchmark_net, trades_benchmark, rebalance_benchmark = simulate_targets(
        market, benchmark, REALISTIC_COSTS, "benchmark_net"
    )
    navs = {
        "q5_net": q5_net,
        "q5_gross": q5_gross,
        "benchmark_net": benchmark_net,
    }
    trades = {
        "q5_net": trades_net,
        "q5_gross": trades_gross,
        "benchmark_net": trades_benchmark,
    }
    rebalances = {
        "q5_net": rebalance_net,
        "q5_gross": rebalance_gross,
        "benchmark_net": rebalance_benchmark,
    }
    performance = build_performance_table(navs, rebalances)
    write_outputs(
        Path(__file__).resolve(),
        summary,
        rank_ic,
        groups,
        signal,
        performance,
        navs,
        trades,
        rebalances,
        quality,
    )
    make_figures(summary, navs, rebalances)
    print(performance.to_string(index=False), flush=True)
    print("PASS: advanced research artifacts written", flush=True)


if __name__ == "__main__":
    main()
