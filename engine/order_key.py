"""OrderKey: structured identity for simulator positions.

Addresses audit P0 #7. Pre-fix, simulator identified positions with:

    f"{instrument}_{entry_epoch}_{exit_epoch}"

Tiered strategies (quality_dip_tiered) emit multiple orders at the same
(instrument, entry_epoch, exit_epoch) but different tier indices — e.g.
entry_config_ids "5_t0", "5_t1", "5_t2". Under the string key, all three
mapped to the same position slot. Combined with `utils.py`'s `_t`-suffix
stripping on the index map, the simulator silently overwrote earlier tiers
with later ones. The symptom: tiered DCA was non-functional out of the box.

The fix: make order identity structured. A frozen dataclass:
  - Is hashable and usable directly as a dict key.
  - Carries entry_config_ids through, so different tiers are distinct.
  - Serializes deterministically to string for log/trade-log output.
  - Fails loudly (TypeError on mutation) if a consumer tries to mutate a key.

See docs/archive/audit-2026-04/AUDIT_FINDINGS.md for the measured impact.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OrderKey:
    """Unique identity for a simulator position.

    Fields:
        instrument: "EXCHANGE:SYMBOL" (e.g. "NSE:TCS").
        entry_epoch: Planned entry epoch (unix seconds).
        exit_epoch: Planned exit epoch at the time the order was generated.
        entry_config_ids: Raw entry_config_ids string from df_orders.
            For tiered strategies this is the distinguishing field
            (e.g. "5_t0" vs "5_t1"). For non-tiered it is the base id
            ("5") and adds no information, but carrying it keeps the key
            uniform.

    Hashability / equality follow from @dataclass(frozen=True): two keys
    are equal iff every field matches.
    """
    instrument: str
    entry_epoch: int
    exit_epoch: int
    entry_config_ids: str = ""

    def __str__(self) -> str:
        """Human-readable serialization for logs and trade_log JSON.

        Format: "{instrument}_{entry_epoch}_{exit_epoch}[@{entry_config_ids}]"
        The "@" prefix on the config ids makes the tier component visible
        when grepping logs.
        """
        base = f"{self.instrument}_{self.entry_epoch}_{self.exit_epoch}"
        if self.entry_config_ids:
            return f"{base}@{self.entry_config_ids}"
        return base

    @classmethod
    def from_order(cls, order: dict) -> "OrderKey":
        """Build from a df_orders row dict or a position dict.

        Both have `instrument`, `entry_epoch`, `exit_epoch`. Only df_orders
        rows have `entry_config_ids`; for positions we stored the key
        whole, so legacy position dicts may not carry it. Missing means
        pre-tier code path — fall back to empty string.
        """
        return cls(
            instrument=order["instrument"],
            entry_epoch=int(order["entry_epoch"]),
            exit_epoch=int(order["exit_epoch"]),
            entry_config_ids=str(order.get("entry_config_ids", "") or ""),
        )
