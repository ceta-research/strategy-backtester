"""What a hosted run's config must be before the engine runs it.

A run is hosted when PIPELINE_CONFIG is set: the platform's run wrapper passes the config there, writes
it to a file and calls run.main() on it. The platform checks a submit from its catalog first; this
check stops a config that skipped or slipped past that check before it reaches a provider's SQL or a
strategy the platform doesn't offer. Ranges, labels and options stay with the platform's catalog: a
string is checked as a plain value, not against an option list. Each param's type is its default's
type in the engine's own builders. Of the static settings, a hosted run takes only those the platform
sends; the engine's execution settings (compute, workers, slippage, filters) keep their defaults.
"""

import math
import os
import re

from engine.config_loader import SWEEP_SECTIONS, build_config, sections, sweep_sizes

STRATEGIES = ("eod_breakout", "eod_technical", "factor_composite", "low_pe", "momentum_cascade",
              "quality_dip_tiered", "trending_value")
PROVIDERS = ("bhavcopy", "cr", "nse_charting")
STATIC = ("data_granularity", "data_provider", "end_epoch", "prefetch_days", "start_epoch", "start_margin",
          "strategy_type")
MAX_COMBINATIONS = 100  # the platform catalog's default cap
SECTIONS = ("static", *SWEEP_SECTIONS)

_PARAM_NAME = re.compile(r"[a-z_][a-z0-9_]{0,63}")
_PLAIN = re.compile(r"[A-Za-z0-9_.:&+-]{0,64}")
_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9&+.-]{0,19}")
_EXCHANGE = re.compile(r"[A-Z0-9]{1,10}")

_PLAIN_WANT = "a plain string: letters, digits and _ . : & + -, at most 64 characters"
_SYMBOL_WANT = "a symbol: A-Z, 0-9, &, +, . or -, at most 20 characters"
_GROUP_WANT = "a non-empty list of {exchange, symbols} objects, one scanner config"
_OVER_CAP = f"at most {MAX_COMBINATIONS} combinations across scanner, entry, exit and simulation"


class ConfigError(ValueError):
    """A config the hosted contract refuses: the field's path ("" for the whole config) and what it
    takes there. It never holds a value that was sent."""

    def __init__(self, field, want):
        super().__init__(f"{field}: {want}" if field else want)
        self.field, self.want = field, want


def is_hosted(environ=os.environ):
    return "PIPELINE_CONFIG" in environ


def check(raw):
    """Raise ConfigError for the first part of raw a hosted run refuses."""
    if not isinstance(raw, dict):
        raise ConfigError("", "an object with sections " + ", ".join(SECTIONS))
    for sec in sorted(raw, key=str):
        if sec not in SECTIONS:
            raise ConfigError(_name(sec), "one of " + ", ".join(SECTIONS))
        if not isinstance(raw[sec], dict):
            raise ConfigError(sec, "an object of params")
    static = raw.get("static", {})
    strategy = static.get("strategy_type")
    if not isinstance(strategy, str) or strategy not in STRATEGIES:
        raise ConfigError("static.strategy_type", "one of " + ", ".join(STRATEGIES))
    defaults = sections(build_config({"static": {"strategy_type": strategy}}))
    for sec in SECTIONS:
        for key in sorted(raw.get(sec, {}), key=str):
            if sec == "static" and key not in STATIC:
                raise ConfigError(f"static.{_name(key)}", "one of " + ", ".join(STATIC))
            if key not in defaults[sec]:
                raise ConfigError(f"{sec}.{_name(key)}", "a param this strategy reads")
            _check_param(sec, key, raw[sec][key], defaults[sec][key])
    provider = static.get("data_provider", defaults["static"]["data_provider"])
    if provider not in PROVIDERS:
        raise ConfigError("static.data_provider", "one of " + ", ".join(PROVIDERS))
    if math.prod(sweep_sizes(build_config(raw)).values()) > MAX_COMBINATIONS:
        raise ConfigError("", _OVER_CAP)


def _name(key):
    """key when it reads as a param name; a stand-in otherwise, so a refusal never repeats input."""
    return key if isinstance(key, str) and _PARAM_NAME.fullmatch(key) else "<unknown key>"


def _check_param(sec, key, value, default):
    field = f"{sec}.{key}"
    if sec == "static":
        _check_value(field, value, default)
        return
    if not isinstance(value, list) or not value:
        raise ConfigError(field, "a list of values, one per sweep step")
    if len(value) > MAX_COMBINATIONS:
        raise ConfigError("", _OVER_CAP)
    if key == "instruments":
        _check_instruments(field, value)
        return
    for i, item in enumerate(value):
        _check_value(f"{field}[{i}]", item, default[0])


def _check_value(field, value, default):
    if isinstance(default, bool):
        ok, want = isinstance(value, bool), "true or false"
    elif isinstance(default, (int, float)):
        ok, want = _is_number(value), "a number"
    elif isinstance(default, str):
        ok, want = isinstance(value, str) and _PLAIN.fullmatch(value), _PLAIN_WANT
    elif isinstance(default, dict):
        _check_dict(field, value, default)
        return
    else:
        ok, want = False, "a value this check knows"
    if not ok:
        raise ConfigError(field, want)


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _check_dict(field, value, default):
    if not isinstance(value, dict) or set(value) != set(default):
        raise ConfigError(field, "an object with keys " + ", ".join(sorted(default)))
    for key in sorted(default):
        _check_value(f"{field}.{key}", value[key], default[key])


def _check_instruments(field, sweep):
    """Each sweep value is one scanner config's list of {exchange, symbols}; one exchange in all."""
    exchanges = set()
    for i, group in enumerate(sweep):
        if not isinstance(group, list) or not group:
            raise ConfigError(f"{field}[{i}]", _GROUP_WANT)
        for j, inst in enumerate(group):
            at = f"{field}[{i}][{j}]"
            if not isinstance(inst, dict) or set(inst) != {"exchange", "symbols"}:
                raise ConfigError(at, "an object with keys exchange, symbols")
            if not isinstance(inst["exchange"], str) or not _EXCHANGE.fullmatch(inst["exchange"]):
                raise ConfigError(f"{at}.exchange", "an exchange code such as NSE")
            if not isinstance(inst["symbols"], list):
                raise ConfigError(f"{at}.symbols", "a list of symbols")
            for k, symbol in enumerate(inst["symbols"]):
                if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
                    raise ConfigError(f"{at}.symbols[{k}]", _SYMBOL_WANT)
            exchanges.add(inst["exchange"])
    if len(exchanges) > 1:
        raise ConfigError(field, "one exchange: the engine adds up each exchange's equity in its own currency")
