"""engine.params_export: the parameters, defaults and options the engine reads, as JSON."""

import json
import subprocess
import sys

import pytest
import yaml

from engine import params_export
from engine.config_loader import load_config
from engine.params_export import ROOT
from engine.signals.base import _STRATEGY_REGISTRY


@pytest.fixture(scope="module")
def exported():
    return params_export.export()


def _loaded(tmp_path, raw):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    return params_export.sections(load_config(str(path)))


def test_shared_sections_are_what_an_empty_config_loads(exported, tmp_path):
    cfg = _loaded(tmp_path, {})
    assert exported["shared"] == {s: cfg[s] for s in ("static", "scanner", "simulation")}


def test_each_registered_strategy_exports_what_its_minimal_config_loads(exported, tmp_path):
    assert sorted(exported["strategies"]) == sorted(_STRATEGY_REGISTRY)
    for name in _STRATEGY_REGISTRY:
        cfg = _loaded(tmp_path, {"static": {"strategy_type": name}})
        assert exported["strategies"][name] == {"entry": cfg["entry"], "exit": cfg["exit"]}, name


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
        for section, values in yaml.safe_load(f.read_text()).items():
            for key, value in values.items():
                assert champion[section][key] == value, (f, section, key)


def test_options_are_the_compared_values_plus_the_else_branch(exported):
    assert exported["options"] == {
        "shared": {
            "simulation": {
                "order_sorting_type": ["top_average_txn", "top_dipper", "top_gainer", "top_performer"],
                "default_sorting_type": ["top_average_txn", "top_gainer"],
            },
            "static": {"data_provider": ["bhavcopy", "cr", "nse_charting", "nse_charting_repaired"]},
        },
        "strategies": {"trending_value": {"entry": {"rebalance_frequency": ["2yearly", "monthly", "quarterly", "yearly"]}}},
    }


def test_each_enum_default_is_one_of_its_options(exported):
    for (strategy, section, name), _ in params_export.DISPATCHES.items():
        owner = exported["shared"] if strategy is None else exported["strategies"][strategy]
        default = owner[section][name]
        default = default[0] if isinstance(default, list) else default
        opts = (exported["options"]["shared"] if strategy is None else exported["options"]["strategies"][strategy])
        assert default in opts[section][name], (strategy, section, name)


def test_every_string_param_is_an_exported_enum_or_says_why_not(exported):
    def strings(section):
        for name, v in section.items():
            if isinstance(v[0] if isinstance(v, list) and v else v, str):
                yield name
    found = {(None, s, n) for s in exported["shared"] for n in strings(exported["shared"][s])}
    found |= {(st, s, n) for st, secs in exported["strategies"].items() for s in secs for n in strings(secs[s])}
    unclassified = {k for k in found if k not in params_export.DISPATCHES and k[2] not in params_export.NOT_OPTIONS}
    assert unclassified == set()


def test_a_dispatch_that_moved_fails_instead_of_exporting_nothing():
    with pytest.raises(LookupError, match="renamed_variable"):
        params_export.dispatch_options(params_export.Dispatch("engine/ranking.py", "sort_orders", "renamed_variable"))
    with pytest.raises(LookupError, match="no_such_function"):
        params_export.dispatch_options(params_export.Dispatch("engine/ranking.py", "no_such_function", "x"))


def test_cli_prints_the_export_as_sorted_json(exported):
    # A child process has its own hash seed, so this also shows set order leaks nowhere.
    out = subprocess.run([sys.executable, "-m", "engine.params_export"], cwd=ROOT,
                         check=True, capture_output=True, text=True).stdout
    assert out == json.dumps(exported, indent=1, sort_keys=True) + "\n"
