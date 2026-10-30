from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import pandas as pd

from src.build_historical_dataset import (
    PRICE_COLUMNS,
    _membership_cache_valid,
    _price_cache_valid,
)
from src.advanced_research import (
    MarketData,
    REALISTIC_COSTS,
    _limit_band,
    _maximum_affordable_notional,
    _trade_commission,
    build_market,
    build_signal_panel,
    period_metrics,
)


def synthetic_market(dates: pd.DatetimeIndex, codes: list[str]) -> MarketData:
    n_dates, n_codes = len(dates), len(codes)
    prices = (
        10
        + np.arange(n_dates, dtype=float)[:, None] * 0.01
        + np.arange(n_codes, dtype=float)[None, :] * 0.1
    )
    frame = pd.DataFrame(prices, index=dates, columns=codes)
    traded = pd.DataFrame(True, index=dates, columns=codes)
    return MarketData(
        calendar=dates,
        codes=codes,
        open=frame.copy(),
        close=frame.copy(),
        close_marked=frame.copy(),
        preclose=frame.copy(),
        amount=pd.DataFrame(50_000_000.0, index=dates, columns=codes),
        traded=traded,
        can_buy=traded.copy(),
        can_sell=traded.copy(),
        limit_up=pd.DataFrame(False, index=dates, columns=codes),
        limit_down=pd.DataFrame(False, index=dates, columns=codes),
        is_st=pd.DataFrame(False, index=dates, columns=codes),
    )


class SignalConstructionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dates = pd.bdate_range("2023-01-02", periods=130)
        self.codes = [f"00000{number}" for number in range(1, 11)]
        self.market = synthetic_market(self.dates, self.codes)

    def membership(self, signal_date: pd.Timestamp) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "query_date": [signal_date] * len(self.codes),
                "index_effective_date": [signal_date] * len(self.codes),
                "code": self.codes,
                "name": ["synthetic"] * len(self.codes),
            }
        )

    def test_future_alignment_is_open_t_plus_1_to_t_plus_6(self) -> None:
        signal_date = self.dates[-10]
        signal = build_signal_panel(
            self.market, self.membership(signal_date)
        )
        expected = (
            self.market.open.at[self.dates[-4], self.codes[0]]
            / self.market.open.at[self.dates[-9], self.codes[0]]
            - 1
        )
        actual = signal.loc[
            signal["code"].eq(self.codes[0]), "forward_open_5"
        ].iloc[0]
        self.assertAlmostEqual(actual, expected)

    def test_missing_future_label_does_not_remove_signal_from_groups(self) -> None:
        signal_date = self.dates[-2]
        signal = build_signal_panel(
            self.market, self.membership(signal_date)
        )
        self.assertEqual(len(signal), len(self.codes))
        self.assertTrue(signal["forward_open_5"].isna().all())
        self.assertEqual(signal["group_20"].nunique(dropna=True), 5)
        self.assertEqual(int(signal["group_20"].eq(5).sum()), 2)
        self.assertTrue(signal["execution_date"].notna().all())


class ExecutionRuleTests(unittest.TestCase):
    def test_commission_and_stamp_duty(self) -> None:
        self.assertAlmostEqual(
            _trade_commission(100_000, REALISTIC_COSTS), 30.0
        )
        self.assertAlmostEqual(
            _trade_commission(1_000, REALISTIC_COSTS), 5.0
        )
        self.assertAlmostEqual(
            REALISTIC_COSTS.stamp_duty(pd.Timestamp("2023-08-27")),
            0.001,
        )
        self.assertAlmostEqual(
            REALISTIC_COSTS.stamp_duty(pd.Timestamp("2023-08-28")),
            0.0005,
        )

    def test_affordable_notional_includes_minimum_commission(self) -> None:
        cash = 10_000.0
        notional = _maximum_affordable_notional(cash, REALISTIC_COSTS)
        all_in = (
            notional
            + _trade_commission(notional, REALISTIC_COSTS)
            + notional * REALISTIC_COSTS.slippage_rate
        )
        self.assertGreater(notional, 0)
        self.assertLessEqual(all_in, cash + 1e-8)
        self.assertGreater(all_in, cash - 0.01)

    def test_board_limit_bands(self) -> None:
        dates = pd.DatetimeIndex(["2020-08-20", "2020-08-24"])
        bands = _limit_band(["600000", "688001", "300001"], dates)
        self.assertAlmostEqual(bands.at[dates[0], "600000"], 0.10)
        self.assertAlmostEqual(bands.at[dates[0], "688001"], 0.20)
        self.assertAlmostEqual(bands.at[dates[0], "300001"], 0.10)
        self.assertAlmostEqual(bands.at[dates[1], "300001"], 0.20)

    def test_open_limit_blocks_matching_side(self) -> None:
        dates = pd.DatetimeIndex(
            ["2024-01-02", "2024-01-03", "2024-01-04"]
        )
        panel = pd.DataFrame(
            {
                "date": dates,
                "code": "600000",
                "open": [11.0, 10.0, 9.0],
                "high": [11.0, 10.0, 9.0],
                "low": [11.0, 10.0, 9.0],
                "close": [11.0, 10.0, 9.0],
                "preclose": [10.0, 10.0, 10.0],
                "volume": [100.0, 100.0, 100.0],
                "amount": [1_000.0, 1_000.0, 1_000.0],
                "turn": [1.0, 1.0, 1.0],
                "tradestatus": [1.0, 1.0, 1.0],
                "isST": [0.0, 0.0, 0.0],
            }
        )
        market = build_market(panel)
        self.assertFalse(market.can_buy.at[dates[0], "600000"])
        self.assertTrue(market.can_sell.at[dates[0], "600000"])
        self.assertTrue(market.can_buy.at[dates[2], "600000"])
        self.assertFalse(market.can_sell.at[dates[2], "600000"])

    def test_max_drawdown_includes_starting_nav(self) -> None:
        nav = pd.Series(
            [1.20, 0.90],
            index=pd.DatetimeIndex(["2024-01-02", "2024-01-03"]),
        )
        metrics = period_metrics(
            nav,
            start=pd.Timestamp("2024-01-02"),
            end=pd.Timestamp("2024-01-03"),
            initial=1.0,
        )
        self.assertAlmostEqual(metrics["max_drawdown"], -0.25)


class CacheValidationTests(unittest.TestCase):
    def test_membership_cache_requires_300_rows_and_matching_date(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "2024-01-02.csv"
            rows = pd.DataFrame(
                {
                    "query_date": ["2024-01-02"] * 300,
                    "index_effective_date": ["2023-12-29"] * 300,
                    "code": [f"{number:06d}" for number in range(300)],
                    "name": ["synthetic"] * 300,
                }
            )
            rows.to_csv(path, index=False)
            self.assertTrue(_membership_cache_valid(path, "2024-01-02"))
            self.assertFalse(_membership_cache_valid(path, "2024-01-03"))
            rows.iloc[:-1].to_csv(path, index=False)
            self.assertFalse(_membership_cache_valid(path, "2024-01-02"))

    def test_price_cache_rejects_wrong_code_and_duplicate_keys(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "600000.parquet"
            rows = pd.DataFrame(
                {
                    "date": ["2024-01-02", "2024-01-03"],
                    "code": ["600000", "600000"],
                }
            )
            for column in sorted(PRICE_COLUMNS - {"date", "code"}):
                rows[column] = 1.0
            rows.to_parquet(path, index=False)
            self.assertTrue(_price_cache_valid(path, "600000"))
            self.assertFalse(_price_cache_valid(path, "600001"))
            rows.loc[1, "date"] = rows.loc[0, "date"]
            rows.to_parquet(path, index=False)
            self.assertFalse(_price_cache_valid(path, "600000"))


if __name__ == "__main__":
    unittest.main()
