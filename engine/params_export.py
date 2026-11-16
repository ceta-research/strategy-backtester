"""The parameters the EOD engine reads, with their defaults and options, as JSON.

    python -m engine.params_export > params.json

A UI or another tool can build its parameter forms from this instead of keeping
its own copy. It reads the engine as it is and changes nothing it runs:
- shared: the static, scanner and simulation sections `load_config` builds for an
  empty config, i.e. every key with its default;
- strategies: each registered strategy's entry and exit sections, built for a
  config that names only that strategy;
- champions: each strategies/<name>/config_champion.yaml as `load_config` loads it;
- options: for each enum param in DISPATCHES, the values its dispatch code compares
  it against, plus the one its else branch runs; nested like shared and strategies.
  Any other value runs as that else branch does;
- not_options: the string params that have no options here, each with the reason.
"""

import ast
import dataclasses
import json
import pathlib
import sys
import tempfile

import yaml

import engine.signals  # noqa: F401  (registers every generator)
from engine.config_loader import load_config
from engine.signals.base import _STRATEGY_REGISTRY

ROOT = pathlib.Path(__file__).resolve().parents[1]
SHARED = ("static", "scanner", "simulation")
OWN = ("entry", "exit")


@dataclasses.dataclass(frozen=True)
class Dispatch:
    """Where the engine branches on an enum param, and what its else branch runs (None: nothing)."""
    path: str
    function: str
    variable: str
    fallback: str | None = None


# (strategy, section, param); strategy None for a shared section.
DISPATCHES = {
    (None, "simulation", "order_sorting_type"): Dispatch("engine/ranking.py", "sort_orders", "order_sorting_type"),
    (None, "simulation", "default_sorting_type"): Dispatch("engine/ranking.py", "sort_orders", "default_sorting_type"),
    (None, "static", "data_provider"): Dispatch("engine/pipeline.py", "run_pipeline", "provider_type", fallback="cr"),
    ("trending_value", "entry", "rebalance_frequency"): Dispatch(
        "engine/signals/trending_value.py", "compute_rebalance_epochs", "frequency", fallback="monthly"),
}

# String params with no options here, by name (or name.key inside a dict param), and why.
NOT_OPTIONS = {
    "strategy_type": "names the generator; the strategies section lists them",
    "data_granularity": "stored in the static config; no EOD code branches on it",
    "regime_instrument": "free text: an instrument such as NSE:NIFTYBEES, or empty",
    "exit_regime_instrument": "free text: an instrument, or empty",
    "fundamental_missing_mode": "an enum whose options aren't exported yet",
    "exit_at": "an enum whose options aren't exported yet",
    "supertrend_mode": "an enum whose options aren't exported yet",
    "exit_fill": "an enum whose options aren't exported yet",
    "stop_mode": "an enum whose options aren't exported yet",
    "max_order_value.type": "an enum whose options aren't exported yet",
}


def sections(cfg):
    """A `load_config` result keyed by the config file's own section names."""
    return {"static": cfg["static_config"], **{s: cfg[f"{s}_config_input"] for s in ("scanner", *OWN, "simulation")}}


def load_raw(raw):
    """`load_config` on a config given as a dict."""
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "config.yaml"
        path.write_text(yaml.safe_dump(raw))
        return sections(load_config(str(path)))


def dispatch_options(dispatch):
    """The string literals `dispatch.variable` is compared with (==) in `dispatch.function`.

    Any other comparison of the variable (`in`, `!=`, the literal on the left) raises,
    so a dispatch the reader can't follow fails here instead of losing options.
    """
    tree = ast.parse((ROOT / dispatch.path).read_text())
    fn = next((n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == dispatch.function), None)
    if fn is None:
        raise LookupError(f"{dispatch.path} has no function {dispatch.function}")
    found = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Compare):
            continue
        names = [n for n in (node.left, *node.comparators) if isinstance(n, ast.Name) and n.id == dispatch.variable]
        if not names:
            continue
        literals = [c for c in node.comparators if isinstance(c, ast.Constant) and isinstance(c.value, str)]
        if node.left is not names[0] or not all(isinstance(op, ast.Eq) for op in node.ops) or len(literals) != len(node.comparators):
            raise LookupError(f"{dispatch.function} in {dispatch.path} compares {dispatch.variable} in a way "
                              f"this reader doesn't follow (line {node.lineno})")
        found |= {c.value for c in literals}
    if not found:
        raise LookupError(f"{dispatch.function} in {dispatch.path} compares {dispatch.variable} with no string")
    return found


def options():
    out = {"shared": {}, "strategies": {}}
    for (strategy, section, name), d in DISPATCHES.items():
        owner = out["shared"] if strategy is None else out["strategies"].setdefault(strategy, {})
        owner.setdefault(section, {})[name] = sorted(dispatch_options(d) | ({d.fallback} if d.fallback else set()))
    return out


def export():
    empty = load_raw({})
    built = {name: load_raw({"static": {"strategy_type": name}}) for name in sorted(_STRATEGY_REGISTRY)}
    return {
        "shared": {s: empty[s] for s in SHARED},
        "strategies": {name: {s: cfg[s] for s in OWN} for name, cfg in built.items()},
        "champions": {f.parent.name: sections(load_config(str(f)))
                      for f in sorted((ROOT / "strategies").glob("*/config_champion.yaml"))},
        "options": options(),
        "not_options": NOT_OPTIONS,
    }


def main():
    json.dump(export(), sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
