"""Benchmark cases: base configs, sweep sizes and the engine's own config count."""

import copy
import json
import os
import tempfile

from engine.config_loader import load_config
from engine.config_sweep import create_config_iterator

HERE = os.path.dirname(os.path.abspath(__file__))
SUITE = os.path.join(HERE, "suite.json")
SIZES = ("1", "10", "50")
SECTIONS = ("scanner", "entry", "exit", "simulation")


def load_suite(path=SUITE):
    with open(path) as f:
        return json.load(f)


def load_base(case):
    with open(os.path.join(HERE, case["config"])) as f:
        return json.load(f)


def build_config(case, size):
    """The base config with the size's sweep axes ("section.param": values) swapped in."""
    cfg = copy.deepcopy(load_base(case))
    for dotted, values in case["sweeps"].get(size, {}).items():
        section, param = dotted.split(".")
        cfg[section][param] = list(values)
    return cfg


def count_configs(cfg):
    """Total combinations as engine/pipeline.py counts them, after the loader's defaults."""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        json.dump(cfg, f)
    try:
        loaded = load_config(f.name)
    finally:
        os.unlink(f.name)
    total = 1
    for section in SECTIONS:
        n, _ = create_config_iterator(**loaded[f"{section}_config_input"])
        total *= n
    return total


def in_window(now, hours, margin_minutes=30):
    """True when a run may start: from start:00 UTC until margin_minutes before end:00 UTC."""
    start, end = hours
    minutes = now.hour * 60 + now.minute
    return start * 60 <= minutes < end * 60 - margin_minutes
