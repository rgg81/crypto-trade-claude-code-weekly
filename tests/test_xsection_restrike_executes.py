"""A stale-stop restrike must CLOSE the held leg, or the gate never re-strikes it.

`stale_stop_restrikes` flags a held leg whose stop is far tighter than current policy (the 2xATR
legacy legs, or a leg whose volatility has since doubled), and the planner re-proposes it as an
OPEN. But the planner never also closed it, and the gate leaves a symbol held on the same side
untouched (executor.reconcile; the explicit-review path keeps every non-force-closed holding). So
the restrike was a silent no-op. Worse, it was a phantom open: the pre-sizer balanced the book
counting that leg's notional, the gate dropped it, and the sleeve came out short.

Live cy591 (2026-10-14): SOL short (9.4% stop, opened cy580) was flagged. The gate opened 13 of 14,
the book came out long-heavy at tilt 0.0346, and the neutrality guard trimmed all 20 longs by 6.68%.

The gate already supports the fix: a FORCE-CLOSED symbol re-proposed in the same cycle is a
legitimate fresh open (cycle.execute_proposals; tests/test_orchestration.py::
test_explicit_close_then_same_direction_reopen). So the planner must emit the close.
"""
from __future__ import annotations

import importlib.util
import json
import math
import sys
from datetime import UTC, datetime
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "xsection_book_cli", Path(__file__).resolve().parents[1] / "scripts" / "xsection_book_cli.py")
xb = importlib.util.module_from_spec(_spec)
sys.modules["xsection_book_cli"] = xb
_spec.loader.exec_module(xb)

_SYMS = [f"S{i}USDT" for i in range(10)]          # S0 weakest ... S9 strongest momentum
NOW = datetime(2026, 10, 14, 16, 20, tzinfo=UTC)


def _rows(i: int, n: int = 300) -> list[list]:
    drift = (i - 4.5) * 0.002
    out, c = [], 100.0
    for t in range(n):
        c *= 1 + drift + 0.03 * math.sin(t * (0.7 + 0.13 * i))   # vol above VOL_FLOOR
        out.append([t * 14_400_000, c, c * 1.01, c * 0.99, c, 0, 0, 0])
    return out


def _plan(tmp_path, monkeypatch, positions: list[dict]) -> tuple[dict, dict]:
    cdir = tmp_path / "cycle" / "6"            # cycle 6, no history -> legacy modulo -> rebalances
    cdir.mkdir(parents=True)
    (cdir / "universe.json").write_text(json.dumps({"universe": [{"symbol": s} for s in _SYMS]}))
    (tmp_path / "positions.json").write_text(json.dumps(positions))
    monkeypatch.setattr(xb, "_klines", lambda sym, *a, **k: _rows(_SYMS.index(sym)))
    monkeypatch.setattr(sys, "argv", ["xsection_book_cli.py", "--cycle", "6",
                                      "--state", str(tmp_path), "--now", NOW.isoformat()])
    xb.main()
    plan = json.loads((cdir / xb.PLAN_FILE).read_text())
    props = json.loads((cdir / "proposals.json").read_text())
    assert plan["rebalancing"] is True
    return plan, props


def _held(sym: str, direction: str, stop_frac: float) -> dict:
    entry = _rows(_SYMS.index(sym))[-1][4]
    stop = entry * (1 - stop_frac) if direction == "long" else entry * (1 + stop_frac)
    return {"symbol": sym, "direction": direction, "entry": entry, "stop": stop, "qty": 1.0}


def test_a_restruck_leg_is_closed_and_reopened(tmp_path, monkeypatch):
    plan, props = _plan(tmp_path, monkeypatch, [_held("S9USDT", "long", 0.02)])
    assert "S9USDT" in plan["plan"]["open"]
    assert "S9USDT" in plan["plan"]["close"], "a restrike that is not closed is a gate no-op"
    closes = {m["symbol"] for m in props["management"] if m["action"] == "close"}
    assert "S9USDT" in closes
    opens = {p["symbol"]: p for p in props["proposals"]}
    assert opens["S9USDT"]["direction"] == "long"
    new_frac = 1 - opens["S9USDT"]["stop"] / opens["S9USDT"]["entry"]
    assert new_frac > 2 * 0.02, "the re-opened leg must carry the WIDER policy stop"


def test_a_restruck_leg_is_never_also_held(tmp_path, monkeypatch):
    """One verdict per symbol: closed+reopened, never also a 'hold' the gate would keep."""
    _, props = _plan(tmp_path, monkeypatch, [_held("S9USDT", "long", 0.02)])
    verdicts = [m["action"] for m in props["management"] if m["symbol"] == "S9USDT"]
    assert verdicts == ["close"], verdicts


def test_a_leg_already_at_policy_stop_is_simply_held(tmp_path, monkeypatch):
    plan, props = _plan(tmp_path, monkeypatch, [_held("S9USDT", "long", 0.30)])
    assert "S9USDT" in plan["plan"]["hold"]
    assert "S9USDT" not in plan["plan"]["open"] and "S9USDT" not in plan["plan"]["close"]
    assert not [p for p in props["proposals"] if p["symbol"] == "S9USDT"]


def test_a_short_restrike_is_closed_and_reopened_too(tmp_path, monkeypatch):
    """Symmetric: the live case was a SHORT (SOL)."""
    plan, props = _plan(tmp_path, monkeypatch, [_held("S0USDT", "short", 0.02)])
    assert "S0USDT" in plan["plan"]["close"] and "S0USDT" in plan["plan"]["open"]
    opens = {p["symbol"]: p for p in props["proposals"]}
    assert opens["S0USDT"]["direction"] == "short"
    assert opens["S0USDT"]["stop"] > opens["S0USDT"]["entry"]


def test_every_open_is_actionable_by_the_gate(tmp_path, monkeypatch):
    """The invariant that was broken: no proposal may target a symbol held on the same side
    unless the same plan force-closes it. Otherwise the gate drops it and the pre-sizer's
    balance is fiction."""
    held = [_held("S9USDT", "long", 0.02), _held("S8USDT", "long", 0.30),
            _held("S0USDT", "short", 0.02)]
    _, props = _plan(tmp_path, monkeypatch, held)
    same_side = {h["symbol"]: h["direction"] for h in held}
    closes = {m["symbol"] for m in props["management"] if m["action"] == "close"}
    for p in props["proposals"]:
        if same_side.get(p["symbol"]) == p["direction"]:
            assert p["symbol"] in closes, f"{p['symbol']} would be a phantom open"
