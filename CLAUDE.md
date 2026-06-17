# CLAUDE.md — Operation TEMPEST-NEUTRAL (autonomous CONSERVATIVE dollar-neutral futures PAPER desk)

This repo is a Claude-native multi-agent trading desk: an orchestrator (Claude running `SKILL.md`)
dispatches a small team — **Momentum** (cross-sectional relative strength/weakness, the edge driver),
**Funding/Basis carry** (a tiebreaker), **Catalyst/News** (events + a market-wide risk-off flag) → a
**CIO/Allocator** that builds a balanced book → **Trader**, plus a **Pace Officer** and **Reflector**,
over a deterministic Python gate (`futures_fund/`) that owns all math/risk/execution. It runs PAPER on
real Binance USD-M mainnet data, on a **single 4h loop**, under a single-flight run lock.

**Mandate: ~3% per MONTH, net of all costs (a CEILING the edge must clear, not a quota). DOLLAR-NEUTRAL
— gross long $ == gross short $ at ~1x gross (no leverage). Conservative: −15% force-flatten.** The
edge is cross-sectional **momentum dispersion** (long relative-strength / short relative-weakness),
with funding **carry** as a secondary tiebreaker — **never short a hot high-funding name to harvest
carry** (Phase-0 lesson: that sleeve net-loses). PAPER ONLY — `live` stays false, forever.

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

### 2. Balanced and cost-aware — the target is a ceiling, not a quota.
Run a DOLLAR-NEUTRAL book: equal gross long and short, momentum-dispersion-led, carry as a tiebreaker.
**Never tilt one-sided to chase pace** (pressing = a balanced-but-fuller book, never a naked sleeve), and
**never admit a fee-negative leg** (expected edge must beat the 0.14% round-trip + adverse funding). A
cost-aware rebalance HOLD overrides a pacing PRESS. **Never press while in drawdown** (anti-martingale —
the breakers own the loss path). Under-performing ~3%/month is acceptable; churning a thin book into
fees or martingaling into the −15% flatten is not.

### 3. Fix every issue in the TEAM SKILL — never work around it by hand.
Any bug, calc error, asymmetry, or missing capability gets fixed by improving the skill — code, agent
prompts, `SKILL.md`, or the lessons corpus — properly (TDD, full suite green). Do NOT patch around a
problem with ad-hoc manual intervention.

### 4. Never hand-edit runtime state.
The orchestrator must NEVER manually edit `state/` (`positions.json`, `account.json`,
`pending_orders.json`, the run lock). If the team needs a capability, build it into the skill.

### 5. Calc-vigilance is always on.
Independently re-derive equity mark-to-market and verify every trade's size / stop / PnL / funding
sign / RR before trusting gate output. Verify the book is actually dollar-neutral (|net|/gross small)
and that funding is signed as a CREDIT when collected. Scrutinize ANY financial math and surface errors.

### 6. Edge net of costs, every time.
A dollar-neutral book has no market beta to lean on — at net-neutral the book grinds to ZERO minus
turnover if the edge is weak. Every leg must clear its round-trip cost (fees + funding + slippage)
AFTER the gate nets it; rebalance only when the realignment edge beats the turnover cost.

### 7. One writer at a time.
The single 4h loop mutates one shared book. Always run under the single-flight lock (`state/.run.lock`);
never run a cycle outside the lock.

### 8. Be proactively alert; report flags without being asked, then turn them into skill improvements.

---

Protected modules (NEVER edit; a fix may not weaken a limit/breaker/safety path): `risk_gate`,
`executor`, `exits`, `consolidation`, `policy`, `liquidation`, `sizing`, `cycle`. The FULL test
suite (`uv run pytest`) and `ruff check .` must pass before any commit. PAPER ONLY: `live` must stay false.
