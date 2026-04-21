"""EquityCurve: self-describing time series of portfolio values.

Addresses P0 audit finding: metrics.py computed `years = n / ppy` where `n` was
the length of a forward-filled (calendar-day) equity curve and `ppy` was 252
(trading days). The mismatch deflated every CAGR by ~1/1.45.

Design contract:
  1. Every EquityCurve knows its sampling frequency. Metrics never guess ppy.
  2. Wall-clock years are always computed from first/last epoch, not sample count.
     CAGR is therefore invariant to forward-fill, sampling rate, and missing bars.
  3. Frequency is used ONLY for volatility annualization (`sqrt(periods_per_year)`),
     where it is definitionally required.

See lib/metrics.py for consumer; tests/test_equity_curve.py for invariants.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Sequence

SECONDS_PER_DAY = 86400
SECONDS_PER_YEAR = 365.25 * SECONDS_PER_DAY


class Frequency(Enum):
    """Sampling frequency of an equity curve.

    The value is the annualization factor used for volatility scaling.
    Trading-day and calendar-day are distinct: a forward-filled curve has
    calendar-day semantics even if it originated from daily bars.
    """
    DAILY_TRADING = 252    # one point per trading day
    DAILY_CALENDAR = 365   # one point per calendar day (forward-filled weekends)
    WEEKLY = 52
    MONTHLY = 12
    QUARTERLY = 4
    ANNUAL = 1
    # Intraday rates quoted per trading-year (6.5h * 60 = 390 1-min bars/day * 252)
    INTRADAY_1MIN = 252 * 390
    INTRADAY_5MIN = 252 * 78

    @property
    def periods_per_year(self) -> int:
        """Annualization factor for vol scaling."""
        return self.value


@dataclass(frozen=True)
class EquityCurve:
    """Time-stamped series of portfolio values.

    Attributes:
        epochs: Unix seconds, strictly increasing.
        values: Portfolio value at each epoch; same length as epochs.
        frequency: Sampling frequency. Drives vol annualization only.

    Invariants (enforced in __post_init__):
      - len(epochs) == len(values)
      - epochs strictly increasing
      - values all finite and >= 0
    """
    epochs: tuple[int, ...]
    values: tuple[float, ...]
    frequency: Frequency

    def __post_init__(self):
        # Coerce sequence inputs to tuples so frozen=True actually delivers
        # immutability. Lists work syntactically but break hashability and
        # can be mutated through an external reference. object.__setattr__
        # is required because the dataclass is frozen.
        if not isinstance(self.epochs, tuple):
            object.__setattr__(self, "epochs", tuple(self.epochs))
        if not isinstance(self.values, tuple):
            object.__setattr__(self, "values", tuple(self.values))

        if len(self.epochs) != len(self.values):
            raise ValueError(
                f"EquityCurve length mismatch: {len(self.epochs)} epochs, "
                f"{len(self.values)} values"
            )
        for i in range(1, len(self.epochs)):
            if self.epochs[i] <= self.epochs[i - 1]:
                raise ValueError(
                    f"EquityCurve epochs must be strictly increasing; "
                    f"epochs[{i-1}]={self.epochs[i-1]} >= epochs[{i}]={self.epochs[i]}"
                )
        for i, v in enumerate(self.values):
            if not math.isfinite(v):
                raise ValueError(f"EquityCurve values[{i}] not finite: {v}")
            if v < 0:
                raise ValueError(f"EquityCurve values[{i}] negative: {v}")

    @classmethod
    def from_pairs(cls, pairs: Sequence[tuple[int, float]],
                   frequency: Frequency) -> "EquityCurve":
        """Build from [(epoch, value), ...]. Convenience for migration from the
        pair-list representation in BacktestResult."""
        if not pairs:
            return cls(epochs=(), values=(), frequency=frequency)
        epochs = tuple(int(e) for e, _ in pairs)
        values = tuple(float(v) for _, v in pairs)
        return cls(epochs=epochs, values=values, frequency=frequency)

    # ── Derived properties ───────────────────────────────────────────────

    def __len__(self) -> int:
        return len(self.epochs)

    @property
    def years(self) -> float:
        """Wall-clock duration in years. Independent of sampling frequency."""
        if len(self.epochs) < 2:
            return 0.0
        return (self.epochs[-1] - self.epochs[0]) / SECONDS_PER_YEAR

    @property
    def total_return(self) -> float:
        """Final / initial - 1. Returns -1.0 on wipeout; raises on empty curve."""
        if len(self.values) < 2:
            raise ValueError("total_return undefined for curve with < 2 points")
        if self.values[0] == 0:
            raise ValueError("total_return undefined when starting value is 0")
        return self.values[-1] / self.values[0] - 1

    def period_returns(self) -> list[float]:
        """Per-period simple returns: [v1/v0 - 1, v2/v1 - 1, ...].

        A forward-filled weekend produces a 0.0 return, which is CORRECT:
        volatility of a zero-return period is zero, and the annualization
        factor (`frequency.periods_per_year`) already accounts for the sampling
        rate. This is the design invariant that makes metrics forward-fill-safe.
        """
        out = []
        for i in range(1, len(self.values)):
            prev = self.values[i - 1]
            if prev <= 0:
                out.append(0.0)
            else:
                out.append(self.values[i] / prev - 1)
        return out
