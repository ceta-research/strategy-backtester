"""The pipeline's result carries the simulator's costs, and no benchmark it doesn't have.

Runs run_pipeline end to end on the synthetic data of tests/test_pipeline.py, with the end date moved
before the data ends so the run has natural exits and positions the simulator closes at the end.
"""

import json
import os
import sys
from types import SimpleNamespace

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import pipeline, simulator  # noqa: E402
from tests.test_pipeline import MINIMAL_CONFIG, FakeProvider  # noqa: E402

DAY = 86400


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    """One pipeline run: its sweep and result, with the simulator's own trade log, final cash and rate."""
    cfg = yaml.safe_load(MINIMAL_CONFIG)
    cfg["static"].update(strategy_type="eod_technical", end_epoch=cfg["static"]["start_epoch"] + 50 * DAY)
    path = tmp_path_factory.mktemp("costs") / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))

    sim = SimpleNamespace()
    process = simulator.process

    def recording(context, df_orders, stats, snapshot, sim_cfg, config_id):
        out = process(context, df_orders, stats, snapshot, sim_cfg, config_id)
        sim.trade_log, sim.cash, sim.rate = out[4], out[2]["margin_available"], context["slippage_rate"]
        return out

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(simulator, "process", recording)
        sweep = pipeline.run_pipeline(str(path), data_provider=FakeProvider())
    assert len(sweep.configs) == 1
    result = sweep.configs[0][1].to_dict()
    return SimpleNamespace(sweep=sweep, result=result, trades=result["trades"], sim=sim,
                           start=cfg["static"]["start_margin"])


def test_the_run_has_natural_exits_and_end_of_run_closes(run):
    reasons = [t["exit_reason"] for t in run.trades]
    assert "end_of_sim" in reasons
    assert any(r != "end_of_sim" for r in reasons)


def test_each_trade_carries_the_simulators_slippage(run):
    assert len(run.trades) == len(run.sim.trade_log)
    for t, logged in zip(run.trades, run.sim.trade_log):
        assert logged["slippage"] > 0
        assert t["slippage"] == pytest.approx(logged["slippage"], abs=0.005)
        assert t["net_pnl"] == pytest.approx(t["gross_pnl"] - t["charges"] - t["slippage"], abs=0.02)


def test_the_cost_totals_add_up_the_trades(run):
    costs = run.result["costs"]
    assert costs["total_slippage"] > 0
    assert costs["total_slippage"] == pytest.approx(sum(t["slippage"] for t in run.trades),
                                                    abs=0.005 * len(run.trades))
    assert costs["total_cost"] == pytest.approx(costs["total_charges"] + costs["total_slippage"], abs=0.01)


def test_trade_pnl_adds_up_to_the_simulators_cash(run):
    net = sum(t["net_pnl"] for t in run.trades)
    assert net == pytest.approx(run.sim.cash - run.start, abs=0.005 * len(run.trades) + 0.01)


def test_trade_pnl_is_the_curve_less_the_end_of_run_sell_costs(run):
    """The curve's last point is the mark-to-market value before the end-of-run close, so it holds none of
    that close's sell charges and sell slippage; the trades closed there do."""
    end_sell_costs = sum(t["sell_charges"] + t["quantity"] * t["exit_price"] * run.sim.rate
                         for t in run.sim.trade_log if t["exit_reason"] == "end_of_sim")
    assert end_sell_costs > 0
    net = sum(t["net_pnl"] for t in run.trades)
    final_value = run.result["summary"]["final_value"]
    assert net == pytest.approx(final_value - run.start - end_sell_costs, abs=0.005 * len(run.trades) + 0.01)


def test_no_benchmark_is_written_without_a_benchmark_series(run, tmp_path):
    assert run.result["benchmark"] is None and run.result["comparison"] is None
    out = tmp_path / "result.json"
    run.sweep.save(str(out), catalog=False)
    for detail in json.loads(out.read_text())["detailed"]:
        assert detail["benchmark"] is None and detail["comparison"] is None
