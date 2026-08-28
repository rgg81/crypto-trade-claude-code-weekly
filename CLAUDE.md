# CLAUDE.md — Operation TEMPEST-NEUTRAL (autonomous CONSERVATIVE dollar-neutral futures PAPER desk)

This repo is a Claude-native multi-agent trading desk: an orchestrator (Claude running `SKILL.md`)
runs a deterministic cross-sectional factor engine — **Momentum** (25-day relative strength across
the top-100 liquid perps, the edge driver), sized inverse-vol and beta-neutralised into a wide
long/short book → **Trader** — over a deterministic Python gate (`futures_fund/`) that owns all math/risk/execution. It runs PAPER on
real Binance USD-M mainnet data, on a **single 4h loop**, under a single-flight run lock.

**Mandate: ~3% per MONTH, net of all costs. DOLLAR-NEUTRAL — gross long $ == gross short $ at ~1x
gross (no leverage) — AND BETA-NEUTRAL: dollar-neutral is not market-neutral, and every factor
tested here carried -0.3 to -0.8 beta until it was explicitly neutralised. Conservative: −15%
force-flatten.**

**The edge is a WIDE CROSS-SECTIONAL MOMENTUM FACTOR BOOK (`futures_fund/xsection.py`,
`scripts/xsection_book_cli.py`). Each cycle the desk ranks the top-100 liquid perps by 150-bar
(25-day) momentum, z-scores that ACROSS the cross-section, scales each leg by 1/volatility,
ITERATIVELY beta-neutralises the book, caps any single name, and holds the strongest ~20 per side —
LONG the top sleeve, SHORT the bottom, equal gross. It rebalances DAILY (every 6th 4h cycle), not
every tick.**

**BREADTH IS THE EDGE, and this is measured, not assumed.** Over 230 perps x 2190 4h bars
(`research/README.md`, `research/xsection_lab.py`), the SAME momentum signal returns:

    legs/side    3      5      8     12     20     25     87
    sharpe      1.19   1.57   1.75   1.90   1.95   2.06   2.15
    max DD     35.3%  26.1%  21.4%  18.5%  13.6%  12.6%  12.8%

`IR = IC * sqrt(breadth)`. The desk's previous 3-leg book was not under-performing because its
signal was weak — a 3-leg book is a punt on the two most extreme names, which in crypto are the most
volatile, and it carried a 35% drawdown that would trip the -15% flatten. At 20 legs/side the same
signal draws down 13.5%. Desk-realistic replay of the shipped construction: **Sharpe ~3.2,
~+7%/month, 13.5% max drawdown, both halves positive.**

Do NOT reintroduce a concentrated book to "sharpen" it, and do NOT judge a signal by its information
coefficient alone — IC measures the middle of the cross-section while a top-N book trades the
extremes, which is exactly how a factor with IC t=+8.5 produced a -2.84 Sharpe portfolio.

PAPER ONLY — `live` stays false, forever.

---

## HARD RULES (non-negotiable)

These override convenience, speed, and token cost. When in doubt, follow them literally.

### 1. The deterministic gate is ABSOLUTE.
`risk_gate`, `sizing`, `liquidation`, `consolidation`, `executor`, `exits`, `policy`, `cycle` are
PROTECTED. The team proposes in price terms; the gate owns sizing, leverage (an OUTPUT, ≤ the regime
cap, ~1x in practice), liq-distance (≥2.5x), RR (≥2), heat, and the circuit breakers (−5% step-down,
−10% reduce-only, −15% force-flatten). No agent and no orchestrator can override it. A code fix here
may NEVER weaken a limit. The dollar-neutral pre-sizer (`neutral_book.py`) and rebalance gate
(`rebalance_cost.py`) are NON-protected and only ever SHRINK risk / advise — they never weaken the gate.

### 2. LEG COUNT IS A SAFETY LIMIT, not a preference.
The book's width is clamped by `xsection_book_cli.fit_n_per_side` to BOTH the gate's live heat
budget and the universe depth. `consolidate()` deletes any leg under 0.001 of equity SILENTLY, so
proposing more than `max_heat/0.001` legs does not trim the book — it quietly loses legs and comes
out lopsided (at the caution tier in `high_vol_range`, max_heat 0.02 allows only 20 legs TOTAL).
Never raise the requested leg count without re-deriving that budget.

### 3. Balanced and cost-aware — the target is a ceiling, not a quota.
Run a DOLLAR-NEUTRAL book: equal gross long and short, momentum-dispersion-led, carry as a tiebreaker.
**Never tilt one-sided to chase pace** (pressing = a balanced-but-fuller book, never a naked sleeve), and
**never admit a fee-negative leg** (expected edge must beat the 0.14% round-trip + adverse funding). A
cost-aware rebalance HOLD overrides a pacing PRESS. **Never press while in drawdown** (anti-martingale —
the breakers own the loss path). Under-performing ~3%/month is acceptable; churning a thin book into
fees or martingaling into the −15% flatten is not.

### 4. Fix every issue in the TEAM SKILL — never work around it by hand.
Any bug, calc error, asymmetry, or missing capability gets fixed by improving the skill — code, agent
prompts, `SKILL.md`, or the lessons corpus — properly (TDD, full suite green). Do NOT patch around a
problem with ad-hoc manual intervention.

### 5. Never hand-edit runtime state.
The orchestrator must NEVER manually edit `state/` (`positions.json`, `account.json`,
`pending_orders.json`, the run lock). If the team needs a capability, build it into the skill.

### 6. Calc-vigilance is always on.
Independently re-derive equity mark-to-market and verify every trade's size / stop / PnL / funding
sign / RR before trusting gate output. Verify the book is actually dollar-neutral (|net|/gross small)
and that funding is signed as a CREDIT when collected. Scrutinize ANY financial math and surface errors.

### 7. Edge net of costs, every time.
A dollar-neutral book has no market beta to lean on — at net-neutral the book grinds to ZERO minus
turnover if the edge is weak. Every leg must clear its round-trip cost (fees + funding + slippage)
AFTER the gate nets it; rebalance only when the realignment edge beats the turnover cost.

### 8. One writer at a time.
The single 4h loop mutates one shared book. Always run under the single-flight lock (`state/.run.lock`);
never run a cycle outside the lock.

### 9. Be proactively alert; report flags without being asked, then turn them into skill improvements.

---

The blended 3-leg score (`blended_score.py`, `blended_book_cli.py`) is SUPERSEDED by the factor book
but retained: its cost gates (`apply_rotation_cost_gate`, the resize cost gate) encode measured
turnover economics, and its history explains the conversion.

Protected modules (NEVER edit; a fix may not weaken a limit/breaker/safety path): `risk_gate`,
`executor`, `exits`, `consolidation`, `policy`, `liquidation`, `sizing`, `cycle`. The FULL test
suite (`uv run pytest`) and `ruff check .` must pass before any commit. PAPER ONLY: `live` must stay false.
