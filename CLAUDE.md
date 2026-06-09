# CLAUDE.md — Operation TEMPEST-WEEKLY (autonomous AGGRESSIVE futures PAPER desk)

This repo is a Claude-native multi-agent trading desk: an orchestrator (Claude running `SKILL.md`)
dispatches a team of **specialist hunter desks** (Momentum/Breakout/Squeeze, Scalper, Funding/Basis
carry, Catalyst/News) → a **CIO/Allocator** → Trader, plus an **Aggression/Pace Officer**, over a
deterministic Python gate (`futures_fund/`) that owns all math/risk/execution. It runs PAPER on real
Binance USD-M mainnet data, on TWO loops — a fast 15m scalp loop and a strategic 4h trend/swing loop
(regime anchored on 4h) — driven by a single serialized poll under a single-flight run lock.

**Mandate: 5% per WEEK, net of all costs. Drawdown-tolerant (~50%). Bold, directional, NOT
market-neutral.** PAPER ONLY — `live` stays false, forever.

---

## HARD RULES (non-negotiable)

These override convenience, speed, and token cost. When in doubt, follow them literally.

### 1. The deterministic gate is ABSOLUTE.
`risk_gate`, `sizing`, `liquidation`, `consolidation`, `executor`, `exits`, `policy`, `cycle` are
PROTECTED. The team proposes in price terms; the gate owns sizing, leverage (an OUTPUT, ≤ the regime
cap), liq-distance (≥2.5x), RR (≥2), heat, and the circuit breakers (-20% step-down, -50% force-
flatten). No agent and no orchestrator can override it. A code fix here may NEVER weaken a limit.

### 2. Bold, not reckless — the target is a goal, not a quota.
ACTIVELY pursue 5%/week: the CIO + Pace Officer deploy hard, press when behind pace, run a one-sided
directional book when a regime pays. BUT: **never press while in drawdown** (anti-martingale — the
breakers own the loss path; pacing only spends UNUSED budget), and never chase a thin setup that
fails the gate. Under-performing the week is acceptable; martingaling into the -50% flatten is not.

### 3. Fix every issue in the TEAM SKILL — never work around it by hand.
Any bug, calc error, asymmetry, or missing capability gets fixed by improving the skill — code, agent
prompts, `SKILL.md`, or the lessons corpus — properly (TDD, full suite green). Do NOT patch around a
problem with ad-hoc manual intervention.

### 4. Never hand-edit runtime state.
The orchestrator must NEVER manually edit `state/` (`positions.json`, `account.json`,
`pending_orders.json`, the run lock). If the team needs a capability, build it into the skill.

### 5. Calc-vigilance is always on.
Independently re-derive equity mark-to-market and verify every trade's size / stop / PnL / funding
sign / RR before trusting gate output. Scrutinize ANY financial math for errors and surface them.

### 6. Edge net of costs, every time.
At 10x and high frequency, fees + funding + slippage compound fast. Every edge — especially a scalp —
must clear its round-trip cost AFTER the gate nets it. A cost-negative desk/loop gets cut, not fed.

### 7. One writer at a time.
Both loops mutate one shared book. Always run under the single-flight lock (`state/.run.lock`); run
the STRATEGIC loop before the FAST loop when both are due. Never run a loop outside the lock.

### 8. Be proactively alert; report flags without being asked, then turn them into skill improvements.

---

Protected modules (NEVER edit; a fix may not weaken a limit/breaker/safety path): `risk_gate`,
`executor`, `exits`, `consolidation`, `policy`, `liquidation`, `sizing`, `cycle`. The FULL test
suite (`uv run pytest`) must pass before any commit. PAPER ONLY: `live` must stay false.
