"""The pipeline's result carries the simulator's costs, and no benchmark it doesn't have.

Runs run_pipeline end to end on the synthetic data of tests/test_pipeline.py, with the end date moved
before the data ends so the run has natural exits and positions the simulator closes at the end.
"""

import json
import os
import sys

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import pipeline, simulator  # noqa: E402
from tests.test_pipeline import MINIMAL_CONFIG, make_deterministic_data  # noqa: E402

DAY = 86400
START = 1577836800


class SyntheticProvider:
    def fetch_ohlcv(self, **kw):
        return make_deterministic_data()


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    """One pipeline run: the sweep, plus each config's own simulator trade log and final snapshot."""
    cfg = yaml.safe_load(MINIMAL_CONFIG)
    cfg["static"].update(strategy_type="eod_technical", end_epoch=START + 50 * DAY)
    path = tmp_path_factory.mktemp("costs") / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))

    books = {}
    process = simulator.process

    def recording(context, df_orders, stats, snapshot, sim_cfg, config_id):
        out = process(context, df_orders, stats, snapshot, sim_cfg, config_id)
        books[config_id] = {"trade_log": out[4], "snapshot": out[2], "rate": context["slippage_rate"]}
        return out

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(simulator, "process", recording)
        sweep = pipeline.run_pipeline(str(path), data_provider=SyntheticProvider())
    params, result = sweep.configs[0]
    return sweep, result.to_dict(), books[params["config_id"]], cfg["static"]["start_margin"]


def test_the_run_has_natural_exits_and_end_of_run_closes(run):
    _, result, _, _ = run
    reasons = [t["exit_reason"] for t in result["trades"]]
    assert "end_of_sim" in reasons
    assert any(r != "end_of_sim" for r in reasons)


def test_each_trade_carries_the_simulators_slippage(run):
    _, result, books, _ = run
    trades, logged = result["trades"], books["trade_log"]
    assert len(trades) == len(logged)
    for t, sim in zip(trades, logged):
        assert sim["slippage"] > 0
        assert t["slippage"] == pytest.approx(sim["slippage"], abs=0.005)
        assert t["net_pnl"] == pytest.approx(t["gross_pnl"] - t["charges"] - t["slippage"], abs=0.02)


def test_the_cost_totals_add_up_the_trades(run):
    _, result, _, _ = run
    costs, trades = result["costs"], result["trades"]
    assert costs["total_slippage"] > 0
    assert costs["total_slippage"] == pytest.approx(sum(t["slippage"] for t in trades), abs=0.005 * len(trades))
    assert costs["total_cost"] == pytest.approx(costs["total_charges"] + costs["total_slippage"], abs=0.01)


def test_trade_pnl_adds_up_to_the_simulators_cash(run):
    _, result, books, start = run
    trades = result["trades"]
    net = sum(t["net_pnl"] for t in trades)
    assert net == pytest.approx(books["snapshot"]["margin_available"] - start, abs=0.005 * len(trades) + 0.01)


def test_trade_pnl_is_the_curve_less_the_end_of_run_sell_costs(run):
    """The curve's last point is the mark-to-market value before the end-of-run close, so it holds none of
    that close's sell charges and sell slippage; the trades closed there do."""
    _, result, books, start = run
    trades = result["trades"]
    end_sell_costs = sum(t["sell_charges"] + t["quantity"] * t["exit_price"] * books["rate"]
                         for t in books["trade_log"] if t["exit_reason"] == "end_of_sim")
    assert end_sell_costs > 0
    final_value = result["summary"]["final_value"]
    net = sum(t["net_pnl"] for t in trades)
    assert net == pytest.approx(final_value - start - end_sell_costs, abs=0.005 * len(trades) + 0.01)


def test_no_benchmark_is_written_without_a_benchmark_series(run, tmp_path):
    sweep, result, _, _ = run
    assert result["benchmark"] is None and result["comparison"] is None
    out = tmp_path / "result.json"
    sweep.save(str(out), catalog=False)
    saved = json.loads(out.read_text())
    for detail in saved["detailed"]:
        assert detail["benchmark"] is None and detail["comparison"] is None
