"""The desk's risk regime was being set by whichever coin sorts FIRST ALPHABETICALLY.

`cycle.py:150` (PROTECTED) sizes the whole book off one symbol's regime:

    caps = caps_for(simple_regime(ctx.frames[ctx.settings.symbols[0]]), health)

auto_cycle never passes `--symbols`, so `gate_execute_cli` builds the gate universe from the cycle's
context briefs — as `sorted(set(...))`. Digits sort first, so `symbols[0]` was a `1000...` meme
coin, not the market. Config's own default (`["BTC/USDT:USDT", "ETH/USDT:USDT"]`) shows BTC-first
was the design; the sort silently discarded it at the TEMPEST-NEUTRAL pivot.

The book meanwhile classified the scout universe's first entry (by 24h volume). So the two sides
read DIFFERENT coins. Live at cy444 (2026-09-13):

    book  -> ETHUSDT      high_vol_range  max_heat 0.04  ptr 0.005  -> clamped to 10 legs/side
    gate  -> 1000BONKUSDT high_vol_trend  max_heat 0.08  ptr 0.010  -> sized legs at 2x the plan

Breadth — the measured edge — was halved for a regime the gate was not even enforcing, and the
caps for the whole desk followed a meme coin's volatility in whichever direction it happened to go.

One ordering, used by BOTH sides: the bellwether (BTC) first when present, everything else in the
scout's order.
"""
from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path

import pandas as pd

from futures_fund.baseline import simple_regime
from futures_fund.bellwether import BELLWETHER, bellwether_first

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "scripts"))

from xsection_book_cli import bellwether_quadrant  # noqa: E402

_spec = importlib.util.spec_from_file_location("gate_execute_cli",
                                               _ROOT / "scripts" / "gate_execute_cli.py")
gate_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate_cli)

ETH, BTC, LSK, BONK = "ETH/USDT:USDT", "BTC/USDT:USDT", "LSK/USDT:USDT", "1000BONK/USDT:USDT"
CY444_BRIEFS = [{"symbol": ETH}, {"symbol": BTC}, {"symbol": LSK}, {"symbol": BONK}]


def _trending(n=300):
    return [100.0 + i * 0.5 for i in range(n)]


def _flat(n=300):
    return [100.0 + (0.01 if i % 2 else -0.01) for i in range(n)]


def _choppy(n=300):
    # +-3% whipsaw around a flat mean: high volatility, no trend -> high_vol_range
    return [100.0 * (1.03 if i % 2 else 0.97) for i in range(n)]


# ---- the shared ordering --------------------------------------------------------------------

def test_the_bellwether_is_btc():
    assert BELLWETHER == BTC


def test_btc_moves_to_the_front_and_everything_else_keeps_its_order():
    assert bellwether_first([ETH, BTC, LSK, BONK]) == [BTC, ETH, LSK, BONK]


def test_without_btc_the_scouts_leader_is_the_bellwether():
    assert bellwether_first([ETH, LSK, BONK]) == [ETH, LSK, BONK]


def test_duplicates_are_dropped_without_reordering():
    assert bellwether_first([ETH, LSK, ETH, BTC, LSK]) == [BTC, ETH, LSK]


def test_an_empty_universe_stays_empty():
    assert bellwether_first([]) == []


# ---- the gate ---------------------------------------------------------------------------------

def test_cy444_the_gate_no_longer_takes_its_regime_from_the_alphabetically_first_coin():
    uni = gate_cli.context_universe({"briefs": CY444_BRIEFS})
    assert uni[0] == BTC, f"gate bellwether was {uni[0]} — alphabetical order sets the desk's caps"
    assert uni == [BTC, ETH, LSK, BONK]


def test_the_gate_still_skips_regime_panel_only_briefs():
    briefs = [{"symbol": ETH}, {"symbol": BTC, "regime_panel_only": True}, {"symbol": LSK}]
    assert gate_cli.context_universe({"briefs": briefs}) == [ETH, LSK]


def test_neither_gate_path_sorts_the_universe():
    """The explicit --symbols path must order the same way, or the bug just moves there."""
    src = inspect.getsource(gate_cli.main)
    assert "sorted(set(" not in src
    assert "bellwether_first(" in src and "context_universe(" in src


# ---- the invariant: book and gate read the SAME coin ----------------------------------------

def test_the_book_classifies_the_same_coin_the_gate_does():
    """THE property. Build a universe where volume-rank first (ETH), alphabetical first (1000BONK)
    and the bellwether (BTC) are all in different regimes, then require the book's quadrant to be
    the quadrant of whatever coin the gate puts first."""
    series = {ETH: _flat(), BTC: _trending(), LSK: _flat(), BONK: _choppy()}
    scout_order = [ETH, BTC, LSK, BONK]
    quad = {s: simple_regime(pd.DataFrame({"close": px})).quadrant for s, px in series.items()}
    assert len({quad[ETH], quad[BTC], quad[BONK]}) == 3, \
        f"scenario must put volume-first, bellwether and alphabetical-first apart: {quad}"

    gate_first = gate_cli.context_universe({"briefs": [{"symbol": s} for s in scout_order]})[0]

    assert bellwether_quadrant(series, scout_order) == quad[gate_first], \
        "the book sized for one regime while the gate enforced another"
