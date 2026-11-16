"""Benchmark cases: base configs, sweep sizes and the engine's own config count."""

import json
import os

from engine.config_sweep import create_config_iterator
from engine.params_export import load_raw

HERE = os.path.dirname(os.path.abspath(__file__))
SUITE = os.path.join(HERE, "suite.json")
SIZES = ("1", "10", "50")
SECTIONS = ("scanner", "entry", "exit", "simulation")


def load_suite(path=SUITE):
    with open(path) as f:
        return json.load(f)


def load_base(name):
    """configs/<name>.json: what the platform's form submits for that strategy when nothing is edited."""
    with open(os.path.join(HERE, "configs", f"{name}.json")) as f:
        return json.load(f)


def build_config(name, case, size):
    """The base config with the size's sweep axes ("section.param": values) swapped in."""
    cfg = load_base(name)
    for dotted, values in case.get(size, {}).items():
        section, param = dotted.split(".")
        cfg[section][param] = list(values)
    return cfg


def count_configs(cfg):
    """Total combinations as engine/pipeline.py counts them, after the loader's defaults."""
    loaded, total = load_raw(cfg), 1
    for section in SECTIONS:
        n, _ = create_config_iterator(**loaded[section])
        total *= n
    return total


def in_window(now, hours, margin_minutes=30):
    """True when a run may start: from start:00 UTC until margin_minutes before end:00 UTC."""
    start, end = hours
    minutes = now.hour * 60 + now.minute
    return start * 60 <= minutes < end * 60 - margin_minutes
