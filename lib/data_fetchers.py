"""Shared data fetching and alignment helpers for standalone strategy scripts."""

import time


def fetch_close(cr, symbol, start_epoch, end_epoch, source="fmp"):
    """Fetch {epoch: close} dict for a symbol.

    Args:
        cr: CetaResearch client instance
        symbol: Ticker symbol (e.g. 'SPY', 'NIFTYBEES')
        start_epoch: Backtest start epoch (warmup is subtracted automatically)
        end_epoch: Backtest end epoch
        source: 'fmp' for FMP stock_eod (adjClose), 'nse' for NSE charting_day
    """
    warmup = start_epoch - 500 * 86400
    if source == "nse":
        sql = f"""SELECT date_epoch, close FROM nse.nse_charting_day
                  WHERE symbol = '{symbol}' AND date_epoch >= {warmup}
                    AND date_epoch <= {end_epoch} ORDER BY date_epoch"""
    else:
        sql = f"""SELECT dateEpoch as date_epoch, adjClose as close FROM fmp.stock_eod
                  WHERE symbol = '{symbol}' AND dateEpoch >= {warmup}
                    AND dateEpoch <= {end_epoch} ORDER BY dateEpoch"""
    for attempt in range(3):
        try:
            return {int(r["date_epoch"]): float(r["close"])
                    for r in cr.query(sql, timeout=180, limit=10000000,
                                      memory_mb=8192, threads=4)
                    if float(r.get("close") or 0) > 0}
        except Exception:
            if attempt < 2:
                time.sleep(5)
            else:
                return {}


def align(datasets, start_epoch):
    """Find sorted common epochs across multiple {epoch: value} dicts."""
    common = sorted(set.intersection(*[set(d.keys()) for d in datasets]))
    return [e for e in common if e >= start_epoch]


def intersect_universes(quality_universe, momentum_universe):
    """Intersect quality and momentum universes epoch-by-epoch.

    Returns dict[epoch, set[symbol]] with only non-empty intersections.
    """
    combined = {}
    all_epochs = set(quality_universe.keys()) | set(momentum_universe.keys())
    for epoch in all_epochs:
        q = quality_universe.get(epoch, set())
        m = momentum_universe.get(epoch, set())
        intersection = q & m
        if intersection:
            combined[epoch] = intersection

    pool_sizes = [len(v) for v in combined.values() if v]
    avg_pool = sum(pool_sizes) / len(pool_sizes) if pool_sizes else 0
    print(f"  Combined universe: {len(combined)} epochs, avg pool={avg_pool:.0f} stocks")
    return combined
