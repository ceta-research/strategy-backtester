"""Tests for engine/config_sweep.py."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.config_sweep import create_config_iterator


def test_basic_cartesian():
    total, gen = create_config_iterator(a=[1, 2], b=[3, 4, 5])
    assert total == 6
    configs = list(gen)
    assert len(configs) == 6
    assert all("id" in c and "a" in c and "b" in c for c in configs)


def test_single_param():
    total, gen = create_config_iterator(x=[10, 20, 30])
    assert total == 3
    configs = list(gen)
    assert len(configs) == 3
    assert configs[0]["x"] == 10
    assert configs[2]["x"] == 30


def test_ids_sequential():
    total, gen = create_config_iterator(a=[1], b=[2], c=[3])
    configs = list(gen)
    assert configs[0]["id"] == 1


def test_empty_raises():
    try:
        create_config_iterator()
        assert False, "Should have raised KeyError"
    except KeyError:
        pass


def test_dict_values():
    """Config values can be dicts (like direction_score)."""
    total, gen = create_config_iterator(
        score=[{"n_day_ma": 3, "score": 0.54}, {"n_day_ma": 5, "score": 0.6}],
        n=[1, 2],
    )
    assert total == 4
    configs = list(gen)
    assert configs[0]["score"] == {"n_day_ma": 3, "score": 0.54}


def test_compound_param_counts_as_one_slot():
    """P2 L133: compound dict params occupy ONE slot in the cartesian
    product — the whole dict is the value. 3 compound × 2 scalar = 6."""
    total, gen = create_config_iterator(
        composite=[
            {"a": 1, "b": 2},
            {"a": 3, "b": 4},
            {"a": 5, "b": 6},
        ],
        x=[10, 20],
    )
    assert total == 6
    configs = list(gen)
    assert len(configs) == 6


def test_empty_list_raises_value_error():
    """P2 L285: commented-out YAML values produce empty lists which
    previously silenced the sweep (zero iterations, no error)."""
    try:
        create_config_iterator(a=[1, 2], b=[])
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "b" in str(e), f"Error should name the offending key: {e}"


def test_empty_list_first_key_also_raises():
    try:
        create_config_iterator(a=[], b=[1])
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "a" in str(e)


if __name__ == "__main__":
    test_basic_cartesian()
    test_single_param()
    test_ids_sequential()
    test_empty_raises()
    test_dict_values()
    test_compound_param_counts_as_one_slot()
    test_empty_list_raises_value_error()
    test_empty_list_first_key_also_raises()
    print("All config_sweep tests passed!")
