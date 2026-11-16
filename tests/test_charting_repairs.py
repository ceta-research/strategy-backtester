"""Tests for engine/charting_repairs.py on a small synthetic charting frame.

AAA prints a raw 2:1 split, BBB has one stray bar from a second price series, and CCC's
history has a gap of more than a year (a recycled ticker).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import polars as pl

from engine import charting_repairs as R
from engine.constants import SECONDS_IN_ONE_DAY

T0 = 1_600_000_000
SPLIT_DAY, STRAY_DAY = 70, 75


def _bars(symbol, days, closes):
    return [{"symbol": symbol, "date_epoch": T0 + d * SECONDS_IN_ONE_DAY, "open": c, "high": c,
             "low": c, "close": c, "average_price": c, "volume": 1000.0} for d, c in zip(days, closes)]


STACKED = [(62, 3.0, 2.0), (74, 7.0, 5.0), (86, 11.0, 10.0)]  # (day, num, den) on DDD


def _stacked_closes():
    closes, level = [], 100.0
    for d in range(100):
        level *= next((den / num for day, num, den in STACKED if day == d), 1.0)
        closes.append(level)
    return closes


def _frame():
    rows = _bars("AAA", range(100), [100.0 if d < SPLIT_DAY else 50.0 for d in range(100)])
    rows += _bars("DDD", range(100), _stacked_closes())
    rows += _bars("BBB", range(100), [7.0 if d == STRAY_DAY else 100.0 for d in range(100)])
    rows += _bars("CCC", list(range(20)) + list(range(420, 440)), [100.0] * 40)
    return pl.DataFrame(rows)


def _splits():
    rows = [("AAA", SPLIT_DAY, 2.0, 1.0)] + [("DDD", day, num, den) for day, num, den in STACKED]
    return pl.DataFrame([{"symbol": s, "ex_epoch": T0 + d * SECONDS_IN_ONE_DAY, "ex_date": "2020-11-23",
                          "num": num, "den": den} for s, d, num, den in rows])


class TestRepair(unittest.TestCase):

    def setUp(self):
        self.df, self.info = R.repair(_frame(), _splits())

    def test_raw_split_is_back_adjusted(self):
        aaa = self.df.filter(pl.col("symbol") == "AAA").sort("date_epoch")
        self.assertEqual(set(aaa["close"].to_list()), {50.0})
        self.assertEqual(aaa["volume"][0], 2000.0)
        aaa_events = self.info["events"].filter(pl.col("symbol") == "AAA")
        self.assertEqual(aaa_events["kind"].to_list(), ["raw"])

    def test_stacked_splits_are_all_back_adjusted(self):
        ddd = self.df.filter(pl.col("symbol") == "DDD").sort("date_epoch")["close"].to_list()
        self.assertEqual(self.info["events"].filter(pl.col("symbol") == "DDD").height, 3)
        for c in ddd:
            self.assertAlmostEqual(c, ddd[-1], places=9)

    def test_stray_bar_is_dropped(self):
        bbb = self.df.filter(pl.col("symbol") == "BBB")
        self.assertEqual(bbb.height, 99)
        self.assertNotIn(7.0, bbb["close"].to_list())
        self.assertEqual(self.info["dropped"]["symbol"].to_list(), ["BBB"])

    def test_gap_splits_the_symbol(self):
        ccc = self.df.filter(pl.col("symbol").str.starts_with("CCC"))
        self.assertEqual(ccc.group_by("symbol", maintain_order=True).len().sort("symbol").rows(),
                         [("CCC", 20), ("CCC~0", 20)])
        self.assertEqual(self.info["stitches"].height, 2)

    def test_row_order_does_not_change_the_result(self):
        """Bit for bit, including DDD's product of three stacked ratios."""
        key = ["symbol", "date_epoch"]
        for seed in range(5):
            df = _frame().sample(fraction=1.0, shuffle=True, seed=seed)
            splits = _splits().sample(fraction=1.0, shuffle=True, seed=seed)
            shuffled, info = R.repair(df, splits)
            self.assertTrue(shuffled.sort(key).equals(self.df.sort(key)), seed)
            self.assertTrue(info["stitches"].equals(self.info["stitches"]), seed)


class TestGroupByKeepsOrder(unittest.TestCase):

    def test_every_group_by_passes_the_audit(self):
        """test_determinism.py's engine-wide audit already fails on another file, so this
        module gets its own check."""
        from tests.test_determinism import TestGroupByMaintainOrderAudit
        self.assertEqual(TestGroupByMaintainOrderAudit()._scan_file(R.__file__), [])


class TestLooksLikeSplit(unittest.TestCase):

    def test_clean_factors_in_either_direction(self):
        self.assertTrue(R.looks_like_split(0.5))
        self.assertTrue(R.looks_like_split(2.0))
        self.assertTrue(R.looks_like_split(1 / 1.1))

    def test_ordinary_moves_are_not_splits(self):
        self.assertFalse(R.looks_like_split(0.97))
        self.assertFalse(R.looks_like_split(0.55))
        self.assertFalse(R.looks_like_split(0))


if __name__ == "__main__":
    unittest.main()
