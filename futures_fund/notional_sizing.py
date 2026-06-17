"""Inverse fixed-fractional sizing for the dollar-neutral pre-sizer (NON-protected).

The gate sizes RISK-first: qty = qty_from_risk(equity, risk_pct, entry, stop) loses exactly
equity*risk_pct at the stop. A dollar-neutral ~1x book instead targets a NOTIONAL per leg
(~equity/2/n per side). This module is the exact algebraic INVERSE: given a target notional, return
the risk_pct that makes qty_from_risk hit it. It reuses sizing.qty_from_risk untouched and never
sizes anything itself — the protected gate remains the sole sizing authority; the pre-sizer only
hands it a risk_mult (clamped to (0,1] by the gate, so it can only ever SHRINK).
"""
from __future__ import annotations


def notional_to_risk_pct(target_notional: float, entry: float, stop: float,
                         equity: float) -> float:
    """risk_pct such that qty_from_risk(equity, risk_pct, entry, stop) * entry == target_notional.

    Derivation: qty_from_risk = equity*risk_pct/|entry-stop|; want qty = target_notional/entry, so
    risk_pct = target_notional * |entry-stop| / (entry * equity). Returns 0.0 on any non-positive
    degenerate input (zero stop distance, zero entry/equity) so a bad leg sizes to nothing, never
    raises. The result may exceed the per-trade cap for a wide stop — the caller clamps the
    risk_mult to (0,1], so a wide-stop leg deploys at the cap (best-effort, never over-deploys).
    """
    risk_per_unit = abs(entry - stop)
    if target_notional <= 0 or equity <= 0 or entry <= 0 or risk_per_unit <= 0:
        return 0.0
    return target_notional * risk_per_unit / (entry * equity)
