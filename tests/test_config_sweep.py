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


if __name__ == "__main__":
    test_basic_cartesian()
    test_single_param()
    test_ids_sequential()
    test_empty_raises()
    test_dict_values()
    print("All config_sweep tests passed!")
