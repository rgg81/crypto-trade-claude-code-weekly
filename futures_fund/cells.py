"""Per-CELL attribution backend — slices the journal into (regime x desk x direction) cells and
computes each cell's OWN statistical edge (DSR). A Tier-2 lesson is a claim about ONE cell
('risk_off momentum shorts net-lose'); gating its promotion on the DESK-WIDE DSR (Phase 1) is too
coarse — a desk profitable overall could still validate a claim about a cell never proven on its
own. Phase 3 gates each lesson on ITS cell's DSR: a cell-specific rule cannot become a standing
RULE until that cell has >=10 closed trades AND a deflated-Sharpe edge. This NEVER feeds the gate —
it only sharpens which agent-judgment priors get promoted to high-confidence.
"""
from __future__ import annotations

from futures_fund.fingerprint import fingerprint_of
from futures_fund.graduation import deflated_sharpe_pvalue
from futures_fund.journal import read_all_decisions

CELL_NUM_TRIALS = 10   # conservative fixed trial count for the deflation (matches scorecard)


def cell_returns(memory_dir, fingerprint: str) -> list[float]:
    """Per-trade returns (realized_pnl / notional) for one cell, in journal order — the series the
    cell's DSR is computed on. Only CLOSED trades with a positive notional contribute."""
    out: list[float] = []
    for d in read_all_decisions(memory_dir):
        if d.get("realized_pnl") is None or fingerprint_of(d) != fingerprint:
            continue
        notional = (d.get("size") or 0.0) * (d.get("entry") or 0.0)
        if notional > 0:
            out.append(d["realized_pnl"] / notional)
    return out


def cell_dsr(memory_dir, fingerprint: str, num_trials: int = CELL_NUM_TRIALS) -> float:
    """DSR p-value for ONE cell's edge. 0.0 below 10 closed trades (the deflated-Sharpe floor) — so
    a cell-specific lesson stays a CANDIDATE until its own cell is statistically proven."""
    return deflated_sharpe_pvalue(cell_returns(memory_dir, fingerprint), num_trials=num_trials)


def cell_table(memory_dir) -> dict[str, dict]:
    """Telemetry: per-fingerprint {n, mean_return, dsr}. For the improvement panel, not gating."""
    by_fp: dict[str, list[float]] = {}
    for d in read_all_decisions(memory_dir):
        if d.get("realized_pnl") is None:
            continue
        notional = (d.get("size") or 0.0) * (d.get("entry") or 0.0)
        if notional > 0:
            by_fp.setdefault(fingerprint_of(d), []).append(d["realized_pnl"] / notional)
    return {fp: {"n": len(rs), "mean_return": (sum(rs) / len(rs)) if rs else 0.0,
                 "dsr": deflated_sharpe_pvalue(rs, num_trials=CELL_NUM_TRIALS)}
            for fp, rs in by_fp.items()}
