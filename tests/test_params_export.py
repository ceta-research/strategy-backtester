"""engine.params_export: the parameters, defaults and options the engine reads, as JSON."""

import json
import subprocess
import sys

import pytest
import yaml

from engine import params_export
from engine.config_loader import UNREAD_STATIC, load_config
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


def _dropped_keys(path):
    """(section, key) of each value the file sets that its loaded config doesn't keep as set, apart from
    the static keys no code reads."""
    loaded = params_export.sections(load_config(str(path)))
    return [(s, k) for s, values in yaml.safe_load(path.read_text()).items() for k, v in values.items()
            if not (s == "static" and k in UNREAD_STATIC) and loaded[s].get(k, _dropped_keys) != v]


def test_champions_are_their_files_loaded_with_every_raw_key_kept(exported):
    files = sorted(ROOT.glob("strategies/*/config_champion.yaml"))
    assert sorted(exported["champions"]) == sorted(f.parent.name for f in files)
    for f in files:
        champion = exported["champions"][f.parent.name]
        assert champion == params_export.sections(load_config(str(f))), f
        assert champion["static"]["strategy_type"] == f.parent.name, f
        assert _dropped_keys(f) == [], f


def test_only_the_unread_static_keys_may_be_dropped(tmp_path):
    path = tmp_path / "config_champion.yaml"
    path.write_text(yaml.safe_dump({"static": {"strategy_type": "low_pe", "data_granularity": "day", "typo_key": 1},
                                    "entry": {"pe_max": [8], "pe_maxx": [9]}}))
    assert sorted(_dropped_keys(path)) == [("entry", "pe_maxx"), ("static", "typo_key")]


def test_data_granularity_is_not_exported(exported):
    assert "data_granularity" not in exported["shared"]["static"]
    assert "data_granularity" not in params_export.NOT_OPTIONS
    assert all("data_granularity" not in c["static"] for c in exported["champions"].values())


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
    def nested(name, v):  # every string in a value, through its lists and dicts: max_order_value.type
        if isinstance(v, str):
            yield name
        elif isinstance(v, list):
            for x in v:
                yield from nested(name, x)
        elif isinstance(v, dict):
            for k, x in v.items():
                yield from nested(f"{name}.{k}", x)

    def strings(section):
        for name, v in section.items():
            yield from nested(name, v)
    found = {(None, s, n) for s in exported["shared"] for n in strings(exported["shared"][s])}
    found |= {(st, s, n) for st, secs in exported["strategies"].items() for s in secs for n in strings(secs[s])}
    unclassified = {k for k in found if k not in params_export.DISPATCHES and k[2] not in params_export.NOT_OPTIONS}
    assert unclassified == set()


def test_not_options_is_exported(exported):
    assert exported["not_options"] == params_export.NOT_OPTIONS


@pytest.mark.parametrize("body", [
    "if mode in ('a', 'b'):\n        pass",
    "if 'a' == mode:\n        pass",
    "if mode != 'a':\n        pass",
    "if mode == other:\n        pass",
])
def test_a_comparison_the_reader_cant_follow_fails(tmp_path, monkeypatch, body):
    (tmp_path / "engine").mkdir()
    (tmp_path / "engine" / "x.py").write_text(f"def f(mode):\n    if mode == 'z':\n        pass\n    {body}\n")
    monkeypatch.setattr(params_export, "ROOT", tmp_path)
    with pytest.raises(LookupError, match="doesn't follow"):
        params_export.dispatch_options(params_export.Dispatch("engine/x.py", "f", "mode"))


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
