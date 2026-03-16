"""Cartesian product config iterator."""

from itertools import product
from typing import Iterator, Tuple


def create_config_iterator(**kwargs) -> Tuple[int, Iterator]:
    """Returns (total_configs, generator) for all parameter combinations.

    Args:
        **kwargs: Each key is a parameter name, value is a list of possible values.

    Returns:
        Tuple of (total_configs, generator yielding dicts with 'id' key + param values)
    """
    if not kwargs:
        raise KeyError("No parameters provided")

    total_configs = 1
    for param_list in kwargs.values():
        total_configs *= len(param_list)

    def config_generator():
        for i, combo in enumerate(product(*kwargs.values()), 1):
            yield {"id": i, **dict(zip(kwargs.keys(), combo))}

    return total_configs, config_generator()
