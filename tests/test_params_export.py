"""engine.params_export: the parameters, defaults and options the engine reads, as JSON."""

import json
import pathlib
import subprocess
import sys

import pytest
import yaml

from engine import params_export
from engine.config_loader import load_config
from engine.signals.base import _STRATEGY_REGISTRY

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def exported():
    return params_export.export()


def _loaded(tmp_path, raw):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    return load_config(str(path))


def test_shared_sections_are_what_an_empty_config_loads(exported, tmp_path):
    cfg = _loaded(tmp_path, {})
    assert exported["shared"] == {"static": cfg["static_config"], "scanner": cfg["scanner_config_input"],
                                  "simulation": cfg["simulation_config_input"]}


@pytest.mark.parametrize("name", ["eod_breakout", "eod_technical", "factor_composite", "low_pe",
                                  "quality_dip_tiered", "trending_value", "momentum_cascade"])
def test_a_strategy_exports_what_its_minimal_config_loads(exported, tmp_path, name):
    cfg = _loaded(tmp_path, {"static": {"strategy_type": name}})
    assert exported["strategies"][name] == {"entry": cfg["entry_config_input"], "exit": cfg["exit_config_input"]}


def test_every_registered_strategy_is_exported(exported):
    assert sorted(exported["strategies"]) == sorted(_STRATEGY_REGISTRY)


def test_spot_defaults(exported):
    assert exported["shared"]["static"]["start_margin"] == 1000000
    assert exported["shared"]["scanner"]["n_day_gain_threshold"] == [{"n": 360, "threshold": 0}]
    assert exported["strategies"]["quality_dip_tiered"]["exit"] == {"trailing_stop_pct": [10], "max_hold_days": [504]}
    # eod_technical has no exit builder of its own: the default one applies.
    assert exported["strategies"]["eod_technical"]["exit"]["trailing_stop_pct"] == [15]


def test_champions_are_their_files_loaded_with_every_raw_key_kept(exported):
    files = sorted(ROOT.glob("strategies/*/config_champion.yaml"))
    assert sorted(exported["champions"]) == sorted(f.parent.name for f in files)
    for f in files:
        champion = exported["champions"][f.parent.name]
        for section, values in (yaml.safe_load(f.read_text()) or {}).items():
            for key, value in (values or {}).items():
                assert champion[section][key] == value, (f, section, key)


def test_options_are_the_values_each_dispatch_compares_against_plus_the_default(exported):
    assert exported["options"] == {
        "simulation.order_sorting_type": ["top_average_txn", "top_dipper", "top_gainer", "top_performer"],
        "simulation.default_sorting_type": ["top_average_txn", "top_gainer"],
        # cr is the else branch, reached by the builder's default
        "static.data_provider": ["bhavcopy", "cr", "nse_charting", "nse_charting_repaired"],
        "trending_value.entry.rebalance_frequency": ["2yearly", "quarterly", "yearly"],
    }


def test_a_dispatch_that_moved_fails_instead_of_exporting_nothing():
    with pytest.raises(LookupError, match="renamed_variable"):
        params_export.dispatch_options(params_export.Dispatch("engine/ranking.py", "sort_orders", "renamed_variable"))
    with pytest.raises(LookupError, match="no_such_function"):
        params_export.dispatch_options(params_export.Dispatch("engine/ranking.py", "no_such_function", "x"))


def test_cli_prints_the_export_as_the_same_json_each_run(exported):
    def run():
        return subprocess.run([sys.executable, "-m", "engine.params_export"], cwd=ROOT,
                              check=True, capture_output=True, text=True).stdout
    first = run()
    assert first == run()
    assert json.loads(first) == json.loads(json.dumps(exported))
