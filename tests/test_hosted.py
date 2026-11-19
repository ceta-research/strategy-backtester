"""A hosted run: the config contract it checks before fetching, its exits, and the files it writes.

A run is hosted when PIPELINE_CONFIG is set (engine/hosted.py). These tests run run.main() through
the benchmark child's copy of the platform wrapper's steps (scripts/benchmark/child.run_hosted).
"""

import copy
import json
import os
import socket
import sys
import tempfile

import polars as pl
import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import run  # noqa: E402
from engine import hosted, pipeline  # noqa: E402
from engine.config_loader import build_config, read_config  # noqa: E402
from engine.signals import factor_composite, low_pe, trending_value  # noqa: E402
from engine.signals.base import DataFetchError  # noqa: E402
from lib import backtest_result  # noqa: E402
from lib.cr_client import CetaResearch, CetaResearchError  # noqa: E402
from scripts.benchmark import child  # noqa: E402
from scripts.benchmark.cases import load_base as form_default  # noqa: E402
from tests.test_pipeline import MINIMAL_CONFIG, make_deterministic_data  # noqa: E402

DELETE = object()


def next_form_default(strategy):
    """The same submit in the platform's next catalog: no data_granularity, n_day_gain_threshold only
    where the strategy reads it, and a 10,000,000 start margin."""
    cfg = form_default(strategy)
    cfg["static"].pop("data_granularity", None)
    if strategy != "eod_technical":
        cfg["scanner"].pop("n_day_gain_threshold", None)
    cfg["static"]["start_margin"] = 10000000
    return cfg


def with_value(cfg, dotted, value):
    """A copy of cfg with section.key (or a top-level key) set to value; DELETE removes it."""
    out = copy.deepcopy(cfg)
    *path, key = dotted.split(".", 1)
    owner = out[path[0]] if path else out
    if value is DELETE:
        del owner[key]
    else:
        owner[key] = value
    return out


def as_wrapper_writes(cfg):
    """cfg as the engine reads it back from the JSON file the wrapper writes."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "pipeline_config.yaml")
        with open(path, "w") as f:
            json.dump(cfg, f)
        return read_config(path)


def test_a_json_config_keeps_floats_yaml_would_read_as_strings(tmp_path):
    path = tmp_path / "pipeline_config.yaml"
    path.write_text(json.dumps({"entry": {"x": [5e-05, 1e21]}}))
    assert yaml.safe_load(path.read_text()) == {"entry": {"x": ["5e-05", "1e+21"]}}
    assert read_config(str(path)) == {"entry": {"x": [5e-05, 1e21]}}


def test_a_yaml_config_reads_as_before(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(MINIMAL_CONFIG)
    assert read_config(str(path)) == yaml.safe_load(MINIMAL_CONFIG)


def nse(*symbols):
    return {"exchange": "NSE", "symbols": list(symbols)}


def refusal(cfg):
    with pytest.raises(hosted.ConfigError) as e:
        hosted.check(as_wrapper_writes(cfg))
    return e.value


# --- the contract --------------------------------------------------------------------------------

CATALOG = ["eod_breakout", "eod_technical", "factor_composite", "low_pe", "momentum_cascade",
           "quality_dip_tiered", "trending_value"]


@pytest.mark.parametrize("strategy", CATALOG)
@pytest.mark.parametrize("shape", [form_default, next_form_default])
def test_form_defaults_pass(strategy, shape):
    hosted.check(as_wrapper_writes(shape(strategy)))


def test_data_granularity_is_taken_and_dropped():
    """The form and older configs send it; the loader keeps no copy, so nothing can branch on it."""
    cfg = form_default("eod_breakout")
    assert cfg["static"]["data_granularity"] == "day"
    hosted.check(as_wrapper_writes(cfg))
    assert "data_granularity" not in build_config(cfg)["static_config"]


def test_the_contract_names_the_catalog():
    assert sorted(hosted.STRATEGIES) == CATALOG
    assert sorted(hosted.PROVIDERS) == ["bhavcopy", "cr", "nse_charting"]
    assert hosted.MAX_COMBINATIONS == 100


SERVER_SENDS = [  # (strategy, section.key, value): values the platform's own check accepts and sends
    ("eod_breakout", "simulation.default_sorting_type",
     ["top_gainer", "top_performer", "gap_size", "volume", "random", "top_average_txn"]),
    ("eod_breakout", "simulation.order_sorting_type",
     ["top_gainer", "top_performer", "gap_size", "volume", "random", "top_dipper"]),
    ("eod_breakout", "simulation.max_order_value",
     [{"type": t, "value": 500000000} for t in ("fixed", "percentage_of_account_value",
                                                "percentage_of_available_margin",
                                                "percentage_of_instrument_avg_txn")]),
    ("eod_breakout", "scanner.price_threshold", [0, 100000, 99.5]),
    ("eod_breakout", "scanner.n_day_gain_threshold", [{"n": 360, "threshold": -999}]),
    ("eod_breakout", "scanner.avg_day_transaction_threshold", [{"period": 125, "threshold": 70000000.0}]),
    ("eod_breakout", "entry.internal_regime_threshold", [0, 1, 0.45, 0.00005]),
    ("eod_breakout", "scanner.avg_day_transaction_threshold", [{"period": 125, "threshold": 1e21}]),
    ("eod_breakout", "scanner.n_day_gain_threshold", [{"n": 360, "threshold": 0.00005}]),
    ("eod_breakout", "entry.force_exit_on_regime_flip", [True, False]),
    ("eod_breakout", "entry.direction_score", [{"n_day_ma": 5, "score": 0}, {"n_day_ma": 10, "score": 0.5}]),
    ("eod_breakout", "entry.regime_instrument",
     ["", "NSE:NIFTYBEES", "NSE:M&M", "NSE:BAJAJ-AUTO", "NSE:SRERAYHY+H"]),
    ("eod_breakout", "exit.trailing_stop_pct", [8, 12.5]),
    ("eod_breakout", "scanner.instruments", [[nse("M&M", "BAJAJ-AUTO", "SRERAYHY+H", "20MICRONS")]]),
    ("eod_breakout", "scanner.instruments", [[nse("TCS"), nse("INFY")]]),
    ("eod_breakout", "scanner.instruments", [[nse("TCS")], [nse("INFY")], [nse()]]),
    ("low_pe", "entry.mktcap_min", [0, 1000000000000]),
    ("low_pe", "entry.roe_min", [0.08, 0.5]),
    ("factor_composite", "exit.vol_target_annual", [0.05, 0.5]),
    ("factor_composite", "entry.factor_weights", [{"gross_profitability": 0.3, "momentum": 0.5, "value": 0.2}]),
    ("trending_value", "entry.rebalance_frequency", ["quarterly", "yearly"]),
    ("trending_value", "entry.growth_weights", [{"earnings": 0.5, "revenue": 0.5}]),
    ("quality_dip_tiered", "entry.min_yearly_return_pct", [0, 0.3]),
]


@pytest.mark.parametrize("strategy,dotted,value", SERVER_SENDS)
def test_what_the_platform_sends_passes(strategy, dotted, value):
    hosted.check(as_wrapper_writes(with_value(form_default(strategy), dotted, value)))


@pytest.mark.parametrize("provider", hosted.PROVIDERS)
def test_each_catalog_provider_passes(provider):
    hosted.check(as_wrapper_writes(with_value(form_default("low_pe"), "static.data_provider", provider)))


def test_the_cap_itself_passes():
    cfg = with_value(form_default("eod_breakout"), "entry.n_day_high", list(range(1, 26)))
    cfg = with_value(cfg, "exit.min_hold_time_days", [0, 1, 2, 3])
    hosted.check(as_wrapper_writes(cfg))


EOD = form_default("eod_breakout")

REFUSED = [  # (name, cfg, field, words the reason holds)
    ("a scalar number in a sweep field", with_value(EOD, "scanner.price_threshold", 99),
     "scanner.price_threshold", "a list"),
    ("a scalar string in a sweep field", with_value(EOD, "simulation.order_sorting_type", "top_gainer"),
     "simulation.order_sorting_type", "a list"),
    ("a dict in a sweep field", with_value(EOD, "entry.direction_score", {"n_day_ma": 5, "score": 0}),
     "entry.direction_score", "a list"),
    ("one-level instruments", with_value(EOD, "scanner.instruments", [nse()]),
     "scanner.instruments[0]", "exchange, symbols"),
    ("an unknown key", with_value(EOD, "entry.n_day_hihg", [3]), "entry.n_day_hihg", "reads"),
    ("an unknown key that isn't a name", with_value(EOD, "entry.look back;", [3]),
     "entry.<unknown key>", "reads"),
    ("another strategy's param", with_value(EOD, "entry.pe_max", [8]), "entry.pe_max", "reads"),
    ("a number for a static string", with_value(EOD, "static.data_granularity", 5),
     "static.data_granularity", "plain string"),
    ("an engine-only static setting", with_value(EOD, "static.cr_api_memory_mb", 65536),
     "static.cr_api_memory_mb", "one of data_granularity, data_provider"),
    ("worker count", with_value(EOD, "static.multiprocessing_workers", 64),
     "static.multiprocessing_workers", "one of data_granularity"),
    ("an unknown section", with_value(EOD, "intraday", {}), "intraday", "one of static"),
    ("an unknown section that isn't a name", with_value(EOD, "dateRange", {}), "<unknown key>", "one of static"),
    ("an unknown provider", with_value(EOD, "static.data_provider", "parquet"),
     "static.data_provider", "one of bhavcopy, cr, nse_charting"),
    ("a provider the catalog doesn't offer", with_value(EOD, "static.data_provider", "nse_charting_repaired"),
     "static.data_provider", "one of bhavcopy"),
    ("a retired strategy", with_value(EOD, "static.strategy_type", "darvas_box"),
     "static.strategy_type", "one of eod_breakout"),
    ("the intraday pipeline", with_value(EOD, "static.strategy_type", "intraday"),
     "static.strategy_type", "one of eod_breakout"),
    ("no strategy", with_value(EOD, "static.strategy_type", DELETE), "static.strategy_type", "one of"),
    ("a symbol holding a quote", with_value(EOD, "scanner.instruments", [[nse("TCS'")]]),
     "scanner.instruments[0][0].symbols[0]", "symbol"),
    ("a symbol holding a semicolon", with_value(EOD, "scanner.instruments", [[nse("TCS", "TCS;DROP")]]),
     "scanner.instruments[0][0].symbols[1]", "symbol"),
    ("a symbol holding a space", with_value(EOD, "scanner.instruments", [[nse("TATA STEEL")]]),
     "scanner.instruments[0][0].symbols[0]", "symbol"),
    ("a string param holding a semicolon", with_value(EOD, "entry.regime_instrument", ["NSE:X;"]),
     "entry.regime_instrument[0]", "plain string"),
    ("an exchange holding a quote", with_value(EOD, "scanner.instruments", [[{"exchange": "NSE'", "symbols": []}]]),
     "scanner.instruments[0][0].exchange", "exchange"),
    ("two currencies in one scanner config",
     with_value(EOD, "scanner.instruments", [[nse(), {"exchange": "LSE", "symbols": []}]]),
     "scanner.instruments", "one exchange"),
    ("two currencies across a sweep",
     with_value(EOD, "scanner.instruments", [[nse()], [{"exchange": "LSE", "symbols": []}]]),
     "scanner.instruments", "one exchange"),
    ("combinations over the cap", with_value(EOD, "entry.n_day_high", list(range(1, 102))),
     "", "at most 100 combinations"),
    ("a product over the cap", with_value(with_value(EOD, "entry.n_day_high", list(range(1, 12))),
                                          "exit.min_hold_time_days", list(range(10))),
     "", "at most 100 combinations"),
    ("a bool for a number", with_value(EOD, "entry.n_day_high", [3, True]), "entry.n_day_high[1]", "a number"),
    ("a string for a number", with_value(EOD, "entry.n_day_high", ["3"]), "entry.n_day_high[0]", "a number"),
    ("NaN for a number", with_value(EOD, "entry.n_day_high", [float("nan")]), "entry.n_day_high[0]", "a number"),
    ("null for a number", with_value(EOD, "entry.n_day_ma", [None]), "entry.n_day_ma[0]", "a number"),
    ("a number for a bool", with_value(EOD, "entry.force_exit_on_regime_flip", [1]),
     "entry.force_exit_on_regime_flip[0]", "true or false"),
    ("a list in a list", with_value(EOD, "entry.n_day_high", [[3]]), "entry.n_day_high[0]", "a number"),
    ("an empty list", with_value(EOD, "entry.n_day_high", []), "entry.n_day_high", "a list"),
    ("a list in static", with_value(EOD, "static.start_margin", [1000000]), "static.start_margin", "a number"),
    ("a dict missing a key", with_value(EOD, "entry.direction_score", [{"n_day_ma": 5}]),
     "entry.direction_score[0]", "keys n_day_ma, score"),
    ("a dict with a word for a number", with_value(EOD, "scanner.avg_day_transaction_threshold",
                                                   [{"period": 125, "threshold": "many"}]),
     "scanner.avg_day_transaction_threshold[0].threshold", "a number"),
    ("a section that isn't an object", with_value(EOD, "entry", []), "entry", "an object"),
    ("a static section that isn't an object", with_value(EOD, "static", None), "static", "an object"),
    ("a start after the end", with_value(EOD, "static.start_epoch", 1773878400),
     "static.start_epoch", "before static.end_epoch"),
    ("no positions", with_value(EOD, "simulation.max_positions", [15, 0]),
     "simulation.max_positions[1]", "above 0"),
    ("no trailing stop on eod_technical", with_value(form_default("eod_technical"), "exit.trailing_stop_pct", [0]),
     "exit.trailing_stop_pct[0]", "above 0"),
    ("an instrument with no symbols key", with_value(EOD, "scanner.instruments", [[{"exchange": "NSE"}]]),
     "scanner.instruments[0][0]", "keys exchange, symbols"),
    ("symbols as a string", with_value(EOD, "scanner.instruments", [[{"exchange": "NSE", "symbols": "TCS"}]]),
     "scanner.instruments[0][0].symbols", "a list of symbols"),
    ("a list past the cap, refused before its values are read",
     with_value(EOD, "entry.n_day_high", ["not a number"] * 101), "", "at most 100 combinations"),
]


def test_a_refusal_reads_field_then_want():
    assert str(hosted.ConfigError("entry.n_day_high[1]", "a number")) == "entry.n_day_high[1]: a number"
    assert str(hosted.ConfigError("", "at most 100 combinations")) == "at most 100 combinations"


def test_a_config_that_isnt_an_object_is_refused():
    for raw in ([], "x", None, 5):
        with pytest.raises(hosted.ConfigError) as e:
            hosted.check(raw)
        assert e.value.field == ""


def test_a_refusal_never_repeats_the_value_sent():
    secret = "SECRETVALUE"
    for cfg in (with_value(EOD, "static.data_provider", secret),
                with_value(EOD, "entry.regime_instrument", [secret + ";"]),
                with_value(EOD, "scanner.instruments", [[nse(secret + "'")]])):
        assert secret not in str(refusal(cfg))


def test_only_the_wrapper_makes_a_run_hosted():
    assert hosted.is_hosted({"PIPELINE_CONFIG": "{}"})
    assert not hosted.is_hosted({"OUTPUT_DIR": "/tmp/run"})


# --- run.py --------------------------------------------------------------------------------------

def run_main(tmp_path, monkeypatch, cfg, is_hosted=True):
    """run.main() through the wrapper's steps, or as a local run.py --config; returns (exit code,
    result path)."""
    monkeypatch.setenv("PIPELINE_CONFIG", "")  # so a value set here is undone after the test
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    out = tmp_path / "result.json"
    code = 0
    try:
        if is_hosted:
            child.run_hosted(cfg, str(tmp_path))
        else:
            monkeypatch.delenv("PIPELINE_CONFIG")
            config_path = tmp_path / "pipeline_config.yaml"
            config_path.write_text(json.dumps(cfg))
            sys.argv = ["run.py", "--config", str(config_path), "--output", str(out)]
            run.main()
    except SystemExit as e:
        code = e.code
    return code, out


def last_stderr_line(capsys):
    lines = capsys.readouterr().err.strip().splitlines()
    return lines[-1] if lines else ""


@pytest.fixture
def no_fetch(monkeypatch):
    """Fail the test if the run reaches the pipeline or opens any network connection."""
    def fetched(*a, **k):
        raise AssertionError("the run fetched data")
    monkeypatch.setattr(run, "run_pipeline", fetched)
    monkeypatch.setattr(socket.socket, "connect", fetched)


@pytest.mark.parametrize("name,cfg,field,words", REFUSED, ids=[r[0] for r in REFUSED])
def test_a_refused_config_exits_2_before_fetching(name, cfg, field, words, tmp_path, monkeypatch, capsys,
                                                  no_fetch):
    code, out = run_main(tmp_path, monkeypatch, cfg)
    line = last_stderr_line(capsys)
    assert code == 2
    assert line.startswith("ConfigError: " + (f"{field}: " if field else ""))
    assert words in line
    assert "/" not in line  # the platform would read a path there and scrub it
    assert not out.exists()


def test_a_local_run_is_not_checked(tmp_path, monkeypatch):
    called = []

    def fake_pipeline(config_path, data_provider=None):
        called.append(config_path)
        return backtest_result.SweepResult("darvas_box", "PORTFOLIO", "NSE", 1000000)
    monkeypatch.setattr(run, "run_pipeline", fake_pipeline)
    code, _ = run_main(tmp_path, monkeypatch, with_value(EOD, "static.strategy_type", "darvas_box"), is_hosted=False)
    assert code == 0 and len(called) == 1


class StubProvider:
    """Stands in for every provider class the pipeline can build: no API call."""
    frame = None

    def __init__(self, *a, **k):
        pass

    def fetch_ohlcv(self, **kw):
        return StubProvider.frame


@pytest.fixture
def stub_data(monkeypatch):
    for name in ("CRDataProvider", "BhavcopyDataProvider", "NseChartingDataProvider"):
        monkeypatch.setattr(pipeline, name, StubProvider)
    StubProvider.frame = make_deterministic_data()
    return StubProvider


def synthetic_config(**static):
    """MINIMAL_CONFIG as a hosted submit for the synthetic data."""
    cfg = yaml.safe_load(MINIMAL_CONFIG)
    cfg["static"].update(strategy_type="eod_technical", data_provider="nse_charting", **static)
    return cfg


def fail_queries(monkeypatch, message="Submit failed (403): forbidden"):
    """Every API query raises CetaResearchError(message)."""
    def failing(*a, **k):
        raise CetaResearchError(message)
    monkeypatch.setenv("CR_API_KEY", "test-only")
    monkeypatch.setattr(CetaResearch, "query", failing)


def low_pe_on_synthetic_data(stub):
    """low_pe over 240 synthetic days, so it has quarterly rebalances and reaches its fundamentals query."""
    base = make_deterministic_data()
    stub.frame = pl.concat([base.with_columns(pl.col("date_epoch") + k * 60 * 86400) for k in range(4)])
    cfg = form_default("low_pe")
    cfg["static"].update(start_epoch=1577836800, end_epoch=1577836800 + 239 * 86400, prefetch_days=0)
    cfg["scanner"]["avg_day_transaction_threshold"] = [{"period": 5, "threshold": 100}]
    return cfg


def empty_data(stub, monkeypatch):
    stub.frame = pl.DataFrame()
    return synthetic_config()


def no_entry_fires(stub, monkeypatch):
    return with_value(synthetic_config(), "entry.direction_score", [{"n_day_ma": 3, "score": 2.0}])


def fundamentals_fail(stub, monkeypatch):
    fail_queries(monkeypatch)
    return low_pe_on_synthetic_data(stub)


def every_order_invalid(stub, monkeypatch):
    monkeypatch.setattr(pipeline, "sanitize_orders", lambda df, **k: df.clear())
    return synthetic_config()


NO_RESULT = [
    (empty_data, "no price data for the config's instruments and dates"),
    (no_entry_fires, "the strategy generated no orders for this data and config"),
    (every_order_invalid, "every order had a zero or missing price"),
    (fundamentals_fail, "the fundamentals query failed (CetaResearchError: Submit failed (403): forbidden)"),
]


@pytest.mark.parametrize("setup,reason", NO_RESULT, ids=[s.__name__ for s, _ in NO_RESULT])
def test_no_result_exits_1_and_says_why(setup, reason, tmp_path, monkeypatch, capsys, stub_data):
    code, out = run_main(tmp_path, monkeypatch, setup(stub_data, monkeypatch))
    assert code == 1 and not out.exists()
    assert last_stderr_line(capsys) == f"NoResultError: {reason}"


def test_a_local_run_with_no_result_still_exits_0(tmp_path, monkeypatch, stub_data):
    code, out = run_main(tmp_path, monkeypatch, empty_data(stub_data, monkeypatch), is_hosted=False)
    assert code == 0 and not out.exists()


def test_the_pipeline_still_returns_an_empty_sweep_with_the_reason(tmp_path, monkeypatch, stub_data):
    path = tmp_path / "config.yaml"
    path.write_text(json.dumps(fundamentals_fail(stub_data, monkeypatch)))
    sweep = pipeline.run_pipeline(str(path))
    assert sweep.configs == []
    assert sweep.no_result_reason.startswith("the fundamentals query failed (CetaResearchError")


FUNDAMENTALS_FETCHES = [low_pe.fetch_pe_fundamentals, trending_value.fetch_quality_growth_data,
                        factor_composite.fetch_fundamentals]


@pytest.mark.parametrize("fetch", FUNDAMENTALS_FETCHES)
def test_each_fundamentals_fetch_raises_when_its_query_fails(fetch, monkeypatch):
    fail_queries(monkeypatch)
    with pytest.raises(DataFetchError, match=r"^the fundamentals query failed \(CetaResearchError: Submit failed"):
        fetch(["NSE"])


@pytest.mark.parametrize("fetch", FUNDAMENTALS_FETCHES)
def test_each_fundamentals_fetch_returns_empty_when_its_query_has_no_rows(fetch, monkeypatch):
    monkeypatch.setenv("CR_API_KEY", "test-only")
    monkeypatch.setattr(CetaResearch, "query", lambda *a, **k: b"")
    assert fetch(["NSE"]).is_empty()


def test_a_long_error_is_cut_to_one_short_line(monkeypatch):
    fail_queries(monkeypatch, "Poll failed (500): " + "x" * 400 + "\nsecond line")
    with pytest.raises(DataFetchError) as e:
        low_pe.fetch_pe_fundamentals(["NSE"])
    assert "\n" not in str(e.value) and "second line" not in str(e.value) and len(str(e.value)) < 200


@pytest.fixture
def catalog_file(tmp_path, monkeypatch):
    path = tmp_path / "catalog" / "catalog.jsonl"
    monkeypatch.setattr(backtest_result, "CATALOG_PATH", str(path))
    return path


def test_a_float_written_in_exponent_form_runs(tmp_path, monkeypatch, stub_data):
    cfg = with_value(synthetic_config(), "entry.internal_regime_threshold", [0.00005])
    code, out = run_main(tmp_path, monkeypatch, cfg)
    assert "5e-05" in (tmp_path / "pipeline_config.yaml").read_text()
    assert code == 0 and out.exists()


def test_a_hosted_run_writes_only_its_result(tmp_path, monkeypatch, stub_data, catalog_file):
    code, out = run_main(tmp_path, monkeypatch, synthetic_config())
    assert code == 0 and out.exists()
    assert not catalog_file.exists()


def test_a_local_run_still_appends_to_the_catalog(tmp_path, monkeypatch, stub_data, catalog_file):
    code, out = run_main(tmp_path, monkeypatch, synthetic_config(), is_hosted=False)
    assert code == 0 and out.exists()
    assert catalog_file.exists()
