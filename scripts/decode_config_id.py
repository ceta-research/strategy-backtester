#!/usr/bin/env python3
"""Decode a config_id (e.g. "1_47_8_2") back to its parameter values, given
the YAML config that produced the sweep.

Usage:
    python scripts/decode_config_id.py <config.yaml> <config_id>
    python scripts/decode_config_id.py strategies/eod_technical/config_holdout_train.yaml 1_47_8_2

The id format is "S_E_X_M" where each is a 1-indexed position into the
itertools.product order of its block's params (scanner/entry/exit/simulation),
matching engine.config_sweep.create_config_iterator.
"""

import argparse
import os
import sys
from itertools import product

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.config_loader import load_config


def _decode(block_input: dict, idx_1based: int) -> dict:
    keys = list(block_input.keys())
    values_lists = [block_input[k] for k in keys]
    combos = list(product(*values_lists))
    if idx_1based < 1 or idx_1based > len(combos):
        return {"_error": f"id {idx_1based} out of range 1..{len(combos)}"}
    return dict(zip(keys, combos[idx_1based - 1]))


def decode(yaml_path: str, config_id: str) -> dict:
    cfg = load_config(yaml_path)
    parts = [int(p) for p in config_id.split("_")]
    if len(parts) != 4:
        raise ValueError(f"config_id must be S_E_X_M: got {config_id}")
    s, e, x, m = parts

    return {
        "config_id": config_id,
        "scanner": _decode(cfg["scanner_config_input"], s),
        "entry": _decode(cfg["entry_config_input"], e),
        "exit": _decode(cfg["exit_config_input"], x),
        "simulation": _decode(cfg["simulation_config_input"], m),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("yaml_path")
    p.add_argument("config_id")
    args = p.parse_args()

    result = decode(args.yaml_path, args.config_id)
    print(f"config_id: {result['config_id']}")
    print()
    for block in ("scanner", "entry", "exit", "simulation"):
        print(f"[{block}]")
        for k, v in result[block].items():
            if k == "id":
                continue
            print(f"  {k}: {v}")
        print()


if __name__ == "__main__":
    main()
