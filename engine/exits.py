"""Canonical exit-condition primitives.

Single authoritative implementation of each exit condition used anywhere
in the backtester. Addresses audit P0s #8, #9, #10:

  P0 #8: `abs(diff) > threshold` anomalous-drop check fired on positive
         gaps (earnings beats, short squeezes), booking losses on days
         stocks actually rallied. Duplicated in two files. Fix: signed
         check, single implementation.

  P0 #9: the anomalous-drop branch in order_generator.py did not record
         its exit to the tracker, so the TSL branch would fire again on
         the next day for the same exit config. Fix: every exit decision
         goes through `record()` which always updates the tracker.

  P0 #10: walk_forward_exit in signals/base.py defaulted
         `require_peak_recovery=True`, silently making TSL never fire
         for breakout-style strategies that didn't know to override.
         Fix: made `require_peak_recovery` a keyword-only mandatory
         argument in walk_forward_exit (see `engine/signals/base.py`).
         Every caller must choose explicitly.

Each detector is a pure function returning `ExitDecision | None`. Callers
use detectors directly when integrating into a more complex loop (as
order_generator.py does). A future `compose_exit_checks` helper may build
on these for simpler call sites, but is not required by the P0 fixes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


ANOMALOUS_DROP_EXIT_PRICE_HAIRCUT = 0.8  # exit at 80% of last close on anomalous drop


@dataclass(frozen=True)
class ExitDecision:
    """Result of an exit-condition check.

    reason: one of {"anomalous_drop", "trailing_stop", "end_of_data",
                    "max_hold"}.
    exit_price: Price at which the exit fires.
    exit_epoch: Epoch at which the exit is booked (may be next_epoch for
                TSL with next-day-open execution, or this_epoch for
                close-of-day exits).
    """
    reason: str
    exit_price: float
    exit_epoch: int


# ── Anomalous gap (signed) ───────────────────────────────────────────────

def anomalous_drop(close_price: float, last_close: float,
                   drop_threshold_pct: float, this_epoch: int) -> Optional[ExitDecision]:
    """Signed drop check. Fires only on significant DOWNWARD moves.

    Args:
        close_price: Today's close.
        last_close: Previous close (reference).
        drop_threshold_pct: Magnitude threshold in percent (e.g. 20.0 for 20%).
        this_epoch: Today's epoch — used for the exit booking.

    Returns:
        ExitDecision at 80% of last_close if price dropped more than
        drop_threshold_pct, else None. Positive gaps never trigger.
        Returns None if last_close is non-positive (undefined ratio).
    """
    if last_close is None or last_close <= 0:
        return None
    diff_pct = (close_price - last_close) * 100.0 / last_close
    # Signed: only negative moves larger than threshold in magnitude.
    if diff_pct < -drop_threshold_pct:
        return ExitDecision(
            reason="anomalous_drop",
            exit_price=last_close * ANOMALOUS_DROP_EXIT_PRICE_HAIRCUT,
            exit_epoch=this_epoch,
        )
    return None


# ── End of data ──────────────────────────────────────────────────────────

def end_of_data(this_epoch: int, last_epoch: int,
                close_price: float) -> Optional[ExitDecision]:
    """Final bar: force-close at the close price."""
    if this_epoch == last_epoch:
        return ExitDecision(reason="end_of_data", exit_price=close_price,
                            exit_epoch=this_epoch)
    return None


# ── Min/max hold gates ───────────────────────────────────────────────────

def below_min_hold(this_epoch: int, entry_epoch: int,
                   min_hold_days: int) -> bool:
    """Return True if we are still within the minimum hold window.
    Callers short-circuit other exit checks while this is True."""
    hold_days = (this_epoch - entry_epoch) / 86400
    return hold_days < min_hold_days


def max_hold_reached(this_epoch: int, entry_epoch: int,
                     max_hold_days: int, close_price: float) -> Optional[ExitDecision]:
    """Fires at close on the bar where hold_days >= max_hold_days.

    Units: `max_hold_days` is CALENDAR days (this_epoch and entry_epoch are
    both unix seconds, and the difference is divided by 86400). This matches
    `engine.signals.base.walk_forward_exit`, `below_min_hold`, and every
    signal generator that uses `max_hold_days` today. Mixing trading-day
    lookbacks with calendar-day exit gates would be a bug — keep one unit.
    max_hold_days == 0 means no max (disabled)."""
    if max_hold_days <= 0:
        return None
    hold_days = (this_epoch - entry_epoch) / 86400
    if hold_days >= max_hold_days:
        return ExitDecision(reason="max_hold", exit_price=close_price,
                            exit_epoch=this_epoch)
    return None


# ── Trailing stop loss ───────────────────────────────────────────────────

def trailing_stop(close_price: float, max_price_since_entry: float,
                  trailing_stop_pct: float,
                  next_epoch: int, next_open: Optional[float],
                  this_epoch: int,
                  entry_price: float = 0.0,
                  tsl_tighten_after_pct: float = 999.0,
                  tsl_tight_pct: float = 0.0) -> Optional[ExitDecision]:
    """TSL: if drawdown from peak since entry exceeds threshold, exit.

    Execution model (MOC-ish): signal at this_epoch's close, exit at
    next_epoch's open if available. Matches `signals/base.walk_forward_exit`
    execution. If next-day open unavailable or non-positive, falls back to
    close at this_epoch.

    trailing_stop_pct == 0 disables TSL (returns None always).

    Adaptive TSL (Phase 5, 2026-04-28): when the trade's MFE from
    entry_price exceeds `tsl_tighten_after_pct`%, the effective TSL
    tightens from `trailing_stop_pct` to `tsl_tight_pct`. Default
    `tsl_tighten_after_pct=999` (disabled) preserves byte-identical
    behavior.
    """
    if trailing_stop_pct <= 0:
        return None
    if max_price_since_entry <= 0:
        return None

    # Adaptive TSL: tighten once MFE exceeds threshold.
    effective_tsl = trailing_stop_pct
    if entry_price > 0 and tsl_tighten_after_pct < 999:
        mfe_from_entry_pct = (max_price_since_entry - entry_price) / entry_price * 100.0
        if mfe_from_entry_pct > tsl_tighten_after_pct:
            effective_tsl = tsl_tight_pct if tsl_tight_pct > 0 else trailing_stop_pct

    drawdown_pct = (max_price_since_entry - close_price) * 100.0 / max_price_since_entry
    if drawdown_pct <= effective_tsl:
        return None
    if next_open is not None and next_open > 0:
        return ExitDecision(reason="trailing_stop", exit_price=next_open,
                            exit_epoch=next_epoch)
    return ExitDecision(reason="trailing_stop", exit_price=close_price,
                        exit_epoch=this_epoch)


# NOTE: A `PeakRecoveryGate` class was drafted here during the Layer 4
# refactor as an OO replacement for `walk_forward_exit`'s inline
# `reached_peak` boolean. Zero callers ever used it. Flagged by two
# independent code reviewers as unused scaffolding; deleted 2026-04-21.
# Re-add only when a concrete caller needs it. The `require_peak_recovery`
# mandatory kwarg in `engine/signals/base.walk_forward_exit` covers the
# P0 #10 fix without the class.


# ── Composition helper for order_generator.py-style flows ────────────────

class ExitTracker:
    """Ensures each exit_config fires at most once per entry.

    Pre-fix, the anomalous-drop branch in order_generator.py recorded
    an exit without adding the exit_config to the tracker — so the TSL
    branch could fire again for the same exit_config on the next bar,
    producing two exit rows with different epochs for one position.
    Post-fix: `record()` is the only way to emit an exit, and it always
    updates the tracker.
    """

    __slots__ = ("_fired",)

    def __init__(self):
        self._fired: set = set()

    def has_fired(self, exit_config_id) -> bool:
        return exit_config_id in self._fired

    def record(self, exit_config_id) -> None:
        self._fired.add(exit_config_id)

    def all_fired(self, total_configs: int) -> bool:
        return len(self._fired) >= total_configs
