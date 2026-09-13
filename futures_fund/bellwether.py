"""The single ordering both the book and the gate use to pick the desk's regime bellwether.

`cycle.py:150` (PROTECTED) sizes the whole book off ONE symbol's regime —
`caps_for(simple_regime(ctx.frames[ctx.settings.symbols[0]]), health)` — so whatever sits first in
the gate universe decides max_heat and per-trade risk for every leg. The book must size for that
same regime, so it has to know which symbol that is.

For five months they disagreed. The gate built its universe with `sorted(set(...))`, which put a
`1000...` meme coin first (digits sort before letters); the book read the scout's volume leader.
At cy444 that meant ETH (high_vol_range, max_heat 0.04) for the book and 1000BONK
(high_vol_trend, max_heat 0.08) for the gate — the book halved its breadth for a regime the gate
was not enforcing, and the desk's caps followed a meme coin.

BTC is the bellwether: config's own default universe is `["BTC/USDT:USDT", "ETH/USDT:USDT"]`, and a
factor book's regime should be the market's, not whichever name tops a volume or alphabetical sort.
When BTC is absent the scout's own leader stands in, identically on both sides.
"""
from __future__ import annotations

BELLWETHER = "BTC/USDT:USDT"


def bellwether_first(symbols: list[str]) -> list[str]:
    """De-duplicate preserving order, with the bellwether moved to the front when present."""
    ordered = list(dict.fromkeys(symbols))
    if BELLWETHER in ordered:
        ordered.remove(BELLWETHER)
        ordered.insert(0, BELLWETHER)
    return ordered
