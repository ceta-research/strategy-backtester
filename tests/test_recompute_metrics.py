"""scripts/recompute_metrics.py rewrites a stored result's summary and leaves its benchmark as stored."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.backtest_result import BacktestResult  # noqa: E402
from scripts import recompute_metrics  # noqa: E402

DAY = 86400
START = 1577836800


def test_a_stored_benchmark_survives_the_rewrite(tmp_path):
    r = BacktestResult("test", {}, "NSE:TEST", "NSE", 1_000_000)
    epochs = [START + i * DAY for i in range(30)]
    for i, e in enumerate(epochs):
        r.add_equity_point(e, 1_000_000 * (1 + 0.003 * i + (0.01 if i % 3 else 0)))
    r.set_benchmark_values(epochs, [100 + 0.2 * i + i % 4 for i in range(30)])
    stored = r.to_dict()
    src, out = tmp_path / "result.json", tmp_path / "out" / "result.json"
    src.write_text(json.dumps(stored))

    report = recompute_metrics.recompute_one(str(src))
    recompute_metrics._write_migrated(str(src), str(out), report["_top_config"], report["_new_portfolio_metrics"],
                                      report["frequency"], {})

    migrated = json.loads(out.read_text())
    assert migrated["benchmark"] == stored["benchmark"]
    assert migrated["comparison"] == stored["comparison"]
    assert "summary_v1_pre_fix" in migrated
