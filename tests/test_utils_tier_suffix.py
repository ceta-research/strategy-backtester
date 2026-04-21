"""Tests for engine.utils.create_config_df_loc_lookup tier-suffix handling (P2 L100).

Pre-audit notes flagged the `_t` suffix strip as fragile — it would
collapse any non-tier config_id that happened to contain `_t` into a
single base id. This test pins the expected behavior so the fragility
cannot regress silently.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import polars as pl

from engine.utils import create_config_df_loc_lookup


class TestTierSuffixStrip(unittest.TestCase):

    def _df(self, entry_ids):
        return pl.DataFrame(
            {
                "scanner_config_ids": ["1"] * len(entry_ids),
                "entry_config_ids": entry_ids,
                "exit_config_ids": ["1"] * len(entry_ids),
            }
        )

    def test_plain_numeric_ids_untouched(self):
        """Non-tier IDs: {'1': {0}, '2': {1}, '3': {2}}."""
        _, entry_map, _ = create_config_df_loc_lookup(
            self._df(["1", "2", "3"])
        )
        self.assertEqual(entry_map[1], {0})
        self.assertEqual(entry_map[2], {1})
        self.assertEqual(entry_map[3], {2})

    def test_tier_suffix_collapses_to_base(self):
        """'5_t1' and '5_t2' both map to base id 5 in the pipeline layer.
        Per-tier uniqueness is carried by OrderKey at the simulator layer."""
        _, entry_map, _ = create_config_df_loc_lookup(
            self._df(["5_t1", "5_t2", "5_t3"])
        )
        self.assertEqual(entry_map[5], {0, 1, 2})

    def test_mixed_tier_and_plain(self):
        _, entry_map, _ = create_config_df_loc_lookup(
            self._df(["1", "2_t1", "2_t2", "3"])
        )
        self.assertEqual(entry_map[1], {0})
        self.assertEqual(entry_map[2], {1, 2})
        self.assertEqual(entry_map[3], {3})

    def test_comma_separated_entry_ids(self):
        """Multiple entry configs on one row — each strip-then-map."""
        _, entry_map, _ = create_config_df_loc_lookup(
            self._df(["1,2_t1,3"])
        )
        self.assertIn(0, entry_map[1])
        self.assertIn(0, entry_map[2])
        self.assertIn(0, entry_map[3])


if __name__ == "__main__":
    unittest.main()
