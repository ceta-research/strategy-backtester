"""The parameters the EOD engine reads, with their defaults and options, as JSON.

    python -m engine.params_export > params.json

A UI or another tool can build its parameter forms from this instead of keeping
its own copy. It reads the engine as it is and changes nothing it runs:
- shared: the static, scanner and simulation sections `load_config` builds for a
  config that names only a strategy, i.e. every key with its default;
- strategies: each registered strategy's entry and exit sections, built the same way;
- champions: each strategies/<name>/config_champion.yaml as `load_config` loads it;
- options: for each enum param, the values its dispatch code compares it against,
  plus its default. Keys are `section.param`, or `strategy.section.param` for a
  param only one strategy reads. A value outside these runs as an unknown value does.
"""

import ast
import dataclasses
import json
import pathlib
import sys
import tempfile

import yaml

from engine.config_loader import load_config

ROOT = pathlib.Path(__file__).resolve().parents[1]
SHARED = ("static", "scanner", "simulation")
OWN = ("entry", "exit")


@dataclasses.dataclass(frozen=True)
class Dispatch:
    """Where the engine branches on an enum param: the function and the variable it compares."""
    path: str
    function: str
    variable: str


DISPATCHES = {
    "simulation.order_sorting_type": Dispatch("engine/ranking.py", "sort_orders", "order_sorting_type"),
    "simulation.default_sorting_type": Dispatch("engine/ranking.py", "sort_orders", "default_sorting_type"),
    "static.data_provider": Dispatch("engine/pipeline.py", "run_pipeline", "provider_type"),
    "trending_value.entry.rebalance_frequency": Dispatch(
        "engine/signals/trending_value.py", "compute_rebalance_epochs", "frequency"),
}


def sections(cfg):
    """A `load_config` result keyed by the config file's own section names."""
    return {"static": cfg["static_config"], **{s: cfg[f"{s}_config_input"] for s in ("scanner", *OWN, "simulation")}}


def load_raw(raw):
    """`load_config` on a config given as a dict."""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml") as f:
        yaml.safe_dump(raw, f)
        f.flush()
        return sections(load_config(f.name))


def dispatch_options(dispatch):
    """The string literals `dispatch.variable` is compared with (==) in `dispatch.function`."""
    tree = ast.parse((ROOT / dispatch.path).read_text())
    fn = next((n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == dispatch.function), None)
    if fn is None:
        raise LookupError(f"{dispatch.path} has no function {dispatch.function}")
    found = {c.value for n in ast.walk(fn) if isinstance(n, ast.Compare)
             and isinstance(n.left, ast.Name) and n.left.id == dispatch.variable
             and all(isinstance(op, ast.Eq) for op in n.ops)
             for c in n.comparators if isinstance(c, ast.Constant) and isinstance(c.value, str)}
    if not found:
        raise LookupError(f"{dispatch.function} in {dispatch.path} compares {dispatch.variable} with no string")
    return found


def _default(shared, strategies, key):
    *owner, section, name = key.split(".")
    value = (strategies[owner[0]] if owner else shared)[section][name]
    return value[0] if isinstance(value, list) else value


def export():
    import engine.signals  # noqa: F401  (registers every generator)
    from engine.signals.base import _STRATEGY_REGISTRY

    empty = load_raw({})
    shared = {s: empty[s] for s in SHARED}
    built = {name: load_raw({"static": {"strategy_type": name}}) for name in sorted(_STRATEGY_REGISTRY)}
    strategies = {name: {s: cfg[s] for s in OWN} for name, cfg in built.items()}
    champions = {f.parent.name: sections(load_config(str(f)))
                 for f in sorted((ROOT / "strategies").glob("*/config_champion.yaml"))}
    options = {key: sorted(dispatch_options(d) | {_default(shared, strategies, key)}) for key, d in DISPATCHES.items()}
    return {"shared": shared, "strategies": strategies, "champions": champions, "options": options}


def main():
    json.dump(export(), sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
