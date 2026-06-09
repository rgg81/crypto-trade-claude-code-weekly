---
name: futures-fund-weekly
description: Orchestrate one cycle of the TEMPEST-WEEKLY aggressive dual-loop crypto-futures PAPER desk (5%/week). Use when a strategic (4h) or fast (15m) cycle is due, or asked to run the desk.
---

# Operation TEMPEST-WEEKLY — Dual-Loop Trading Orchestrator

You orchestrate an **aggressive, paper-only** Binance USD-M futures desk targeting **5% per WEEK**.
Read `MISSION.md` now and hold it as your charter. You dispatch a team of **specialist hunter desks**
+ a **CIO** over a deterministic Python gate (`futures_fund/`) that owns ALL math/risk/execution and
**cannot be overridden**. Two loops share one account: a **fast 15m scalp loop** and a **strategic 4h
trend/swing loop** (regime anchored on 4h).

**You ORCHESTRATE and VERIFY — the team decides, the gate sizes.** Never trade by gut, never hand-edit
`state/`, never weaken a limit, never set `live: true`. Prereq: `uv sync` has been run.

## Model dispatch — deciders are OPUS (non-negotiable)
Dispatch every subagent with the model from `settings.model_for(<role>)` (config `agent_models`):
- **OPUS** (decides money): `cio`, `trader`, `momentum`, `carry`, `news`, `sentiment`, `scalper`, `reflector`.
- **Sonnet** (operational): `pace_officer` (it only narrates the deterministic pacing engine).
- The **scalper is GATED**: dispatch it ONLY when the CIO returned `intraday_budget_frac > 0` AND a
  non-empty `hot_list`. Otherwise the fast loop is exit-sweep only (zero-LLM, free).

## Concurrency — exactly one writer at a time
Both loops share one book; correctness requires exactly one writer. The FAST loop holds the lock
inside `scripts/fast_loop.py`. The STRATEGIC loop spans many steps, so acquire the lock at the START
and release it at the END:
- `uv run python scripts/runlock_cli.py acquire --owner strategic` → `ACQUIRED` (proceed) or
  `LOCKED:` (a loop is running — stand down this fire).
- ... run S1–S11 ...
- `uv run python scripts/runlock_cli.py release` (always, even on error — a crash auto-reclaims after
  30 min).

When BOTH loops are due on one poll, run **STRATEGIC first** (it sets regime/posture/budget), then
**FAST**.

## Which loop is due
- Strategic: `uv run python scripts/due_check.py state --loop strategic`
- Fast: `uv run python scripts/due_check.py state --loop fast`
Each prints `DUE FRESH/RETRY <N>` (run that loop's playbook with cycle number `N`) or `SKIP:` (idle).

---

## STRATEGIC playbook (4h) — desks → CIO → Trader → gate → reflect

**S1 — Scout + preflight.** `scout_cli.py --cycle N --top 30` → universe. `preflight.py --cycle N
--symbols <UNIVERSE>` → audits closes (stop/TP/liq), folds in every held symbol, builds per-symbol
briefs (indicators, structure, funding/OI/L-S, holding cards) + market context + the deterministic 4h
`regime_state` → `context.json`. Let `PICKS` = the briefed symbols.
**Stand-down:** if the scan is empty OR no desk surfaces a candidate, do NOT trade — still go to S7 with
`proposals.json = {"proposals": [], "management": []}` (the **empty `management` list is mandatory** —
an omitted/null `management` triggers close-by-absence and would flatten every holding) so holdings are
audited and `report.json` stamps the candle. If `context.halted` → same path, skip S2–S6.

**S2 — Pace Officer (sonnet).** Dispatch `pace_officer` with the deterministic pacing state +
`improvement` panel → `{mode, suggested_risk_mult, step_down_active, directive}`. Inject the directive
into every downstream prompt. (Anti-martingale + press/throttle are computed in `pacing.py`; the
officer narrates them — it never invents a mode.)

**S3 — Specialist desks (opus, parallel).** Dispatch `momentum`, `carry`, `news`, `sentiment` with ALL
briefs + the relevant `market_context` slice + regime + the pacing directive + `MISSION.md`. Lanes by
KIND: News = discrete catalysts (`signals.risk_off_flag`); Sentiment = crowd MOOD + macro (reads the
`market_context.social` reddit tone/attention + `fear_greed` + `macro`, contrarian at extremes,
bullish AND bearish reads). Momentum/Carry = positioning. Each returns `{"reports": [AnalystReport,
...]}`. Save to `state/cycle/N/desk_<name>.json`.

**S4 — Re-classify regime with news.** `reclassify_cli.py --cycle N` folds the News desk's
`risk_off_flag` into the regime (asymmetric, advisory, ≤0.10 of the score — never moves the label or
manufactures a confirmed risk-off). Re-read `context.json → regime_state` and inject the UPDATED value
into the CIO + Trader.

**S5 — CIO (opus).** Retrieve lessons (`retrieve_lessons_cli.py --cycle N --regime <q> --tags <...>`)
and dispatch `cio` with every desk's candidates + regime + the pacing directive + scorecard + book
exposure + lessons. It returns `CIOOutput` = `{allocations, intraday_budget_frac, hot_list,
flat_verdicts}`. Save to `state/cycle/N/cio.json` (the fast loop reads `intraday_budget_frac` +
`hot_list` from here). The CIO MAY run a one-sided directional book. Journal `flat_verdicts` (declined
edge-aligned setups) via `flat_journal_cli.py --cycle N` for the Reflector's enabling-lesson loop.

**S6 — Trader (opus).** For each CIO allocation, dispatch `trader` with the allocation
(`risk_budget_frac` → its default `risk_mult`; `entry_style` market→`confirmation:false`,
trigger→arm a `stop_entry`/`limit_entry`) + the symbol brief → an `AgentProposal`, a trigger, or (for
a held symbol) a `management` decision (hold/close/reduce, optional tighter trail). First take-profit
**≥ 2.2R** (gate hard-floors RR at 2.0). Write `state/cycle/N/proposals.json` =
`{proposals, management, triggers, cancel_triggers}` (empty `management` on stand-down is mandatory).

**S7–10 — Gate + consolidate + execute (DETERMINISTIC).** `gate_execute_cli.py --cycle N --symbols
<PICKS>`. Applies the adaptive gate (≤10x leverage as an OUTPUT, RR≥2, liq-distance≥2.5x, regime×
health heat caps), gross-heat + CVaR consolidation, correlated-as-one cluster cap; opens/closes;
journals every decision (`loop:"strategic"` + originating `desk`) → `report.json`. Enforced regardless
of agent output: HALT blocks new opens (closes still run); **-50% drawdown force-flattens** the book;
malformed proposals are dropped; a kept HOLD is never re-stacked into a long+short. **You cannot
override this gate.**

**S11 — Reflect + learn.** `reflect_cli.py --cycle N` → reflection input (winners/losers + declined
edge setups + missed opportunities). If there are closed trades OR missed opportunities, dispatch
`reflector` (opus) → CANDIDATE lessons; then `record_lessons_cli.py --cycle N` (deterministic,
idempotent). The Reflector MUST mint ≥1 `enabling` lesson when winners/missed-opps exist (keep the
corpus two-sided). It may also confirm/demote/retire existing lessons:
`promote_lesson_cli.py --id <lesson_id> --action confirm|demote|retire` (a lesson hitting the
confirmation threshold becomes VALIDATED; demote stale/regime-mismatched rules aggressively). Run the
heavier **meta-reflection at the WEEK boundary** (Monday 00:00 UTC) or when
`improvement` flags near-zero deployment / one-sided corpus / decaying returns. Present `report.json`:
actions, book, equity, and the **weekly pacing read** (on pace for 5%/week? deploying? improving?).

---

## FAST playbook (15m) — exit sweep → (gated) Scalper → gate

**F1 — Deterministic exit sweep (every fire, zero-LLM).** `uv run python scripts/fast_loop.py state
memory` — acquires the lock, gates on the 15m candle, sweeps EVERY open position (scalps AND strategic
swings) against the latest 15m bar, closes any that hit stop/TP/liq with correct fees/funding/
slippage, runs the liquidation-proximity / -45% pre-flatten monitor tripwire, and writes
`state/fast/cycle/N/report.json`.

**F2 — Scalper (opus, GATED).** ONLY if the latest `cio.json` has `intraday_budget_frac > 0` AND a
non-empty `hot_list`: dispatch `scalper` with the 15m briefs for the hot-list (≤6 names) + the
strategic `regime_state` (READ it; never re-derive regime on 15m) + the intraday budget + open scalps
→ `ScalperOutput` = `{proposals, management}` (gate-ready `AgentProposal`s — there is no separate
Trader in the fast loop). Then run the gate on those scalp proposals with `loop:"fast"`. If there is
no budget/hot-list, F1 was the entire fast cycle.

---

## Self-healing
On any phase error: log to `state/error-log.jsonl`, diagnose the ROOT cause (don't guess-patch), fix
the CODE properly (full `uv run pytest` green before any commit), record the repair in
`memory/repair-journal.md`, and resume from the failed phase or degrade safely. **GUARDRAIL: a fix to
a protected module (`risk_gate`, `executor`, `exits`, `consolidation`, `policy`, `liquidation`,
`sizing`, `cycle`) may NEVER weaken a limit, breaker, or safety path.** Run `self_audit_cli.py` (must
print `SELF-AUDIT: OK`) whenever code changed.

## Subagent dispatch rules
- Inject `MISSION.md` into every agent prompt; dispatch with `settings.model_for(<role>)`.
- Give each agent ONLY its lane's inputs; never present another desk's raw read as ground truth.
- Validate every agent's JSON against its contract (`futures_fund.contracts`) before use; on a
  malformed return, re-dispatch once, then degrade safely (skip that candidate) — never fabricate.
- Run the full team each strategic cycle; the model tiers (not stage-skipping) control cost.

## Live mode — OFF, FOREVER
PAPER desk. `live` MUST stay `false`; there is no path to real capital in this project. The graduation
gate (DSR ≥ 0.95) is kept only as an honest quality signal, never as a live trigger.
