# 🌩️ Operation TEMPEST-WEEKLY

**An aggressive, self-improving multi-agent crypto-futures PAPER desk — built as a Claude Code skill.**

> *We are an autonomous crypto-futures PAPER desk with one mandate: compound the account at 5% every WEEK — net of every fee, every funding payment, every slip — by hunting any profitable situation in the Binance USD-M futures market. Bold, not reckless: edge is earned, the deterministic risk gate is absolute, and leverage is the **output** of our risk, never the input.*
> — [`MISSION.md`](MISSION.md)

A team of specialized LLM **hunter desks** — Momentum/Breakout/Squeeze, Scalper, Funding/Basis Carry, Catalyst/News — surfaces trades; a **CIO/Allocator** ranks them and deploys the weekly risk budget; deterministic, unit-tested Python owns **all** math, risk limits, and execution. It runs on **two loops** sharing one account — a fast **15m** scalp loop and a strategic **4h** trend/swing loop — on **Binance USD-M perpetual futures**, PAPER on real mainnet data.

`661 tests · ruff-clean · Python 3.11 · PAPER-only`

---

## ⚠️ Disclaimer

This is a **research / educational** project and an **aggressive** one: it targets a very high return (5%/week) and **deliberately tolerates deep drawdowns (~50%)**. It is **not financial advice**, makes **no guarantee** of profit, and ships with **no warranty**. It is **PAPER-ONLY** — `live` is hard-disabled and there is no path to real capital here. 5%/week net is brutally hard and almost certainly not sustainable; the desk is designed to **under-perform gracefully rather than martingale into ruin**.

---

## How it works — the dual-loop firm

The **orchestrator** (Claude running [`SKILL.md`](SKILL.md)) runs whichever loop is due, under a single-flight lock (one writer at a time; strategic before fast when both fire).

**STRATEGIC loop (4h) — find the edge, allocate, trade:**
```
 Scout + Preflight ... universe · close stop/TP/liq hits · briefs (RSI/ADX/funding/OI/L-S) · 4h regime
        │
 Pace Officer ........ weekly press/throttle posture (narrates the deterministic pacing engine)
        │
 Hunter desks ....... MOMENTUM · CARRY · NEWS  (parallel — each surfaces its best trades)
        │
 CIO / Allocator .... ranks all candidates → allocates the WEEKLY risk budget → market/trigger
        │
 Trader ............. allocation → concrete order (entry, ATR stop, ≥2.2R target, confirmation)
        │
 ┌──────────────────────────────────────────────────────────────────────────────┐
 │ RISK GATE  (deterministic — ≤10x leverage as OUTPUT, RR≥2, liq-dist≥2.5x, heat)│ ← the survival
 │ CONSOLIDATION  (gross-heat cap · CVaR de-risk · correlated-as-one)             │    layer; the LLM
 │ EXECUTION + JOURNAL  (paper fills with fees/funding/slippage; loop+desk tagged) │    cannot override
 └──────────────────────────────────────────────────────────────────────────────┘
        │
 Reflect ............ realized PnL → lessons (gated promotion); weekly meta-reflection
```

**FAST loop (15m) — manage and scalp:**
```
 Exit sweep (every fire, zero-LLM) ... close any position hitting stop/TP/liq at 15m resolution,
                                       with fees/funding/slippage · liq-proximity / -45% tripwire
        │
 Scalper (Opus, GATED) ............... ONLY when the CIO granted intraday budget + a hot-list →
                                       gate-ready 15m scalps + management → the same risk gate
```

## The team

| Agent | Role | Model |
|---|---|---|
| **Momentum / Breakout / Squeeze** | Trends, breakouts, crowded-short squeeze longs, crowded-long flush shorts | Opus |
| **Funding / Basis Carry** | Harvest funding-rate extremes + basis carry (the steady contributor) | Opus |
| **Catalyst / News** | Discrete events (listings/hacks/ETF/reg) + the desk-wide risk-off flag + macro tone | Opus |
| **Scalper** | 15m micro-trend / band-edge mean-reversion; emits gate-ready orders directly | Opus *(gated)* |
| **CIO / Allocator** | Ranks every desk's ideas, allocates the weekly risk budget, runs a one-sided book | Opus |
| **Trader** | Converts a CIO allocation into an order (ATR stop, ≥2.2R, market/trigger) | Opus |
| **Aggression / Pace Officer** | Weekly press/throttle posture + the anti-martingale conscience | Sonnet |
| **Reflector** | Post-close attribution → lessons (promoted only when statistically proven) | Opus |
| **Risk + Portfolio Managers** *(deterministic)* | The hard gate + book consolidation — **code, not persuasion** | — |

**Rule: every agent that decides money runs on Opus** (`settings.model_for`); only operational agents run cheaper. The Scalper is Opus but its dispatch is gated — it only fires when the CIO actually handed it budget + a hot-list.

## Design principles (the non-negotiables)

- **Bold, not reckless.** The 5%/week target is a *goal, not a quota*. The desk presses hard when behind pace — but **never while in drawdown** (anti-martingale; the breakers own the loss path), and never on a setup that fails the gate.
- **The risk gate is code, not vibes.** Position size comes from the ATR stop; **leverage is an output**, never an input. Aggressive caps (≤10x, 2–3% risk/trade, 30–40% heat) but the survival invariants stay: `RR≥2`, liq-distance `≥2.5x`, and a **−50% drawdown force-flattens** the book.
- **Edge is net of costs or it's fiction.** Every PnL accounts for maker/taker fees, funding, and slippage — critical at 10x and high frequency.
- **One writer at a time.** Both loops share one book under a single-flight lock; the strategic loop runs first.
- **Remember honestly; self-improve & self-heal.** Decisions are journaled *before* the outcome; a code "fix" may **never** weaken a risk limit.

## Architecture

```
SKILL.md              orchestrator playbook — TWO playbooks (strategic + fast)
MISSION.md            the charter, injected into every agent prompt
agents/*.md           momentum · carry · news · scalper · cio · pace_officer · trader · reflector
                        + risk_manager/portfolio_manager (deterministic-gate docs)
                        (retired debate agents live in agents/archive/)
futures_fund/         the Python engine, grouped by responsibility:
  · risk core         models · costs · liquidation · sizing · portfolio_risk · policy · risk_gate
  · scheduling        scheduling (floor_tf · multi-cadence cycle_due) · runlock (single-flight lock)
  · the loops         fills · exits · executor · consolidation · cycle · orchestration · fast_loop
  · data layer        config · exchange · market_data · vendors
  · state & memory    state · portfolio · journal · hitrate · lessons · pacing · playbook
  · evaluation        equity_log · metrics · graduation · scorecard · improvement · self_audit
  · vendor/           regime_detection · feature_engineering · walk_forward · overfit_detector
scripts/              CLIs: due_check (--loop) · fast_loop · runlock_cli · scout · preflight ·
                        reclassify · retrieve_lessons · gate_execute (--loop) · reflect ·
                        record_lessons · promote_lesson · monitor · scorecard · self_audit
tests/                661 offline tests (no network, no live LLM)
```

## Quality & testing

- **661 tests, 100% offline** — pure functions on synthetic data; the gate/exits on fixtures; a **dual-loop integration test** proves the strategic open + fast sweep share one book under the lock; role files locked to their JSON contracts by a schema-conformance harness.
- **Live data path smoke-validated** against real Binance USD-M mainnet (scout + preflight build real briefs + regime; no orders, no keys, paper).
- The aggressive envelope is fully parameter-driven; the deterministic survival layer (`risk_gate`, `sizing`, `liquidation`, `executor`, `exits`, `consolidation`, `policy`, `cycle`) is **protected** — a fix may never weaken a limit.

## Quickstart (paper)

Requires [`uv`](https://docs.astral.sh/uv/) and Python 3.11+.

```bash
uv sync                       # install deps + create the venv
uv run pytest                 # 661 tests, all offline
uv run ruff check .

# Which loop is due? (read-only, no network)
uv run python scripts/due_check.py state --loop strategic
uv run python scripts/due_check.py state --loop fast

# Fast loop (deterministic exit sweep; safe to run any time):
uv run python scripts/fast_loop.py state memory

# Full LLM team: invoke the futures-fund-weekly skill (follow SKILL.md) inside Claude Code.
```

Config lives in [`config.yaml`](config.yaml): `target_weekly`, `max_drawdown_tolerance`, per-loop `timeframe`/`poll_minutes`, and `agent_models` (the deciders-are-Opus map). `live` must stay `false`.

## Scheduling

A single poll (`*/5 * * * *`) fires the dual-loop runner: acquire the lock → `due_check --loop strategic` / `--loop fast` → run whichever is due (strategic first). The fast loop's exit sweep also serves as the between-tick risk monitor (liquidation-proximity + −45% pre-flatten HALT tripwire).

### Kill switch
```bash
uv run python -c "from futures_fund.state import set_halt; set_halt('state', True, reason='manual kill')"
```
Trips the **HALT** flag immediately: new opens are blocked, de-risking closes still run, and the next cycle short-circuits. Clear with `set_halt('state', False)`. (This is PAPER — the kill switch protects the simulated book; there is no real-capital go-live path in this project.)

## Memory & learning

Git-versioned, auditable memory under `memory/`: a **two-phase decision journal** (written before the outcome, patched on close, tagged with `loop` + `desk`), a **lessons corpus** (CANDIDATE → VALIDATED only on recurrence + statistical support; two-sided enabling/restrictive), **per-agent hit-rates**, and a **repair journal**.

## Status

| | |
|---|---|
| ✅ Aggressive risk envelope · weekly pacing · dual-cadence scheduling + lock | built & tested |
| ✅ Fast-loop 15m exit sweep · specialist-desk roster + CIO · two-playbook orchestrator | built & tested |
| ✅ Live data path (scout + preflight on real mainnet data) | smoke-validated |
| ⏳ Live end-to-end paper run on a schedule (cron) | up to the operator |

PAPER-only by design — there is no real-capital path in this project.

## Credits & inspiration

Forked from the monthly **Operation TEMPEST** desk and re-tuned for an aggressive weekly mandate. Distilled from research into multi-agent LLM trading systems — TradingAgents, FINCON, FinMem, QuantAgent — grounded in Binance USD-M futures mechanics. Built with [Claude Code](https://claude.com/claude-code).

*No license is set — by default "all rights reserved."*
