---
name: futures-fund-neutral
description: Orchestrate one cycle of the TEMPEST-NEUTRAL conservative dollar-neutral crypto-futures PAPER desk (~3%/month). Use when the single 4h strategic cycle is due, or asked to run the desk.
---

# Operation TEMPEST-NEUTRAL — Single-Loop Dollar-Neutral Orchestrator

You orchestrate a **conservative, paper-only** Binance USD-M futures desk running a **DOLLAR-NEUTRAL**
long/short book (~1x gross, gross long $ == gross short $) targeting **~3% per MONTH** net of cost.
Read `MISSION.md` now and hold it as your charter. You dispatch a small team — **Momentum** (the edge
driver: cross-sectional relative strength/weakness), **Carry** (tiebreaker), **News** → a **CIO** that
builds the balanced book → **Trader** — over a deterministic Python gate (`futures_fund/`) that owns
ALL math/risk/execution and **cannot be overridden**. There is **one loop: the strategic 4h cycle**
(regime anchored on 4h). There is no fast loop and no scalper; exits are swept at the 4h cycle.

**You ORCHESTRATE and VERIFY — the team decides, the gate sizes.** Never trade by gut, never hand-edit
`state/`, never weaken a limit, never set `live: true`. Verify the book is actually dollar-neutral
(|net|/gross small) and no leg is fee-negative. Prereq: `uv sync` has been run.

## Model dispatch — deciders are OPUS (non-negotiable)
Dispatch every subagent with the model from `settings.model_for(<role>)` (config `agent_models`):
- **OPUS** (decides money): `cio`, `trader`, `momentum`, `carry`, `news`, `sentiment`, `reflector`.
- **Sonnet** (operational): `pace_officer` (it only narrates the deterministic pacing engine).

## Concurrency — exactly one writer at a time
The single 4h loop spans many steps, so acquire the lock at the START and release it at the END:
- `uv run python scripts/runlock_cli.py acquire --owner strategic` → `ACQUIRED` (proceed) or
  `LOCKED:` (a cycle is running — stand down this fire).
- ... run S1–S11 ...
- `uv run python scripts/runlock_cli.py release` (always, even on error — a crash auto-reclaims after
  30 min).

## Which cycle is due
- `uv run python scripts/run_loops.py` (acquires the lock, prints `strategic.due`), or
- `uv run python scripts/due_check.py state --loop strategic`
Prints `DUE FRESH/RETRY <N>` (run the playbook with cycle number `N`) or `SKIP:` (idle).

---

## STRATEGIC playbook (4h) — desks → CIO → Trader → gate → reflect

**S1 — Scout + preflight.** `scout_cli.py --cycle N --top 30` → universe. `preflight.py --cycle N
--symbols <UNIVERSE>` → audits closes (stop/TP/liq), folds in every held symbol, builds per-symbol
briefs (indicators, structure, funding/OI/L-S, holding cards) + market context + the deterministic 4h
`regime_state` → `context.json`. `context.json` ALSO carries two JUDGMENT-ONLY learning blocks the
gate NEVER reads: (1) the **read-gated, honestly-tagged `lessons`** (Tier-2 statistical RULES) —
validated `[RULE · …]` + proven-enough `[CANDIDATE — unproven (n=, conf=) · …]` (thin one-off cohorts
withheld); and (2) **`episodic`** (Tier-1 descriptive) — the desk's WORST realised outcomes per
setup fingerprint (regime × desk × direction), most-dangerous first, as an ANTI-PRESS tail brake.
Every decision agent reads both. Let `PICKS` = the briefed symbols.
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

**S5 — CIO (opus).** Dispatch `cio` with every desk's candidates + regime + the pacing directive +
scorecard + book exposure + the `lessons` already in `context.json` (S1 injected them — no separate
retrieve step needed; `retrieve_lessons_cli.py --cycle N --regime <q> --tags <...>` remains available
to manually inspect/re-pull the read-gated corpus). It returns `CIOOutput` = `{allocations,
intraday_budget_frac, hot_list,
flat_verdicts}`. Save to `state/cycle/N/cio.json` (`intraday_budget_frac` is 0 and `hot_list` empty —
no scalper). The CIO builds a DOLLAR-NEUTRAL book (balanced long/short sleeves); it MUST NOT run a
one-sided book. The deterministic pre-sizer (`neutral_book.py`) balances the allocations to equal
dollars before the gate. Its `flat_verdicts` (declined
edge-aligned setups, each ideally with `edge_aligned`/`favored_side`) are journaled **automatically by
the gate step** (S7–10) for the enabling-lesson loop — no manual `flat_journal_cli` call.

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
override this gate.** On the strategic loop this step ALSO runs the **closed learning loop** (all
fail-safe — a learning bug can never break trading): (1) **attribution** stamps desk × regime ×
close_reason × `r_multiple` onto the just-closed journal rows; (2) the **flat-verdict bridge** journals
the CIO's `flat_verdicts`; (3) the **deterministic reflect-runner** mines two-sided cohort candidates
from the recent per-cohort window (count + cycle-recency bounded), CONFIRMS recurring ones, and
promotes only when that cohort's **OWN cell** is statistically proven (**per-cell DSR≥0.95 + ≥5
distinct cycles**) — demotes a validated rule its cohort just reversed, and runs an **asymmetric TTL
sweep** (stale candidates retired; stale `enabling` 'press' rules demoted to re-prove; a working
`restrictive` brake is never expired on silence). This grows the corpus every cycle without depending
on the LLM Reflector remembering to write.

**S11 — Reflect + learn (QUALITATIVE layer).** The deterministic statistical layer ALREADY ran in
S7–10 (cohort win/loss candidates, confirm/promote/demote). S11 adds what the machine is blind to: the
*causal, narrative* lesson. `reflect_cli.py --cycle N` → reflection input (winners/losers + declined
edge setups + missed opportunities). If there are closed trades OR missed opportunities, dispatch
`reflector` (opus) → CANDIDATE lessons (`source:curated`, NOT read-gated — trust the LLM's reasoned
lesson); then `record_lessons_cli.py --cycle N` (deterministic, idempotent). Do NOT just restate cohort
counts (the miner has those) — mint the WHY. The Reflector MUST mint ≥1 `enabling` lesson when
winners/missed-opps exist (keep the corpus two-sided). It may also confirm/demote/retire existing
lessons:
`promote_lesson_cli.py --id <lesson_id> --action confirm|demote|retire` (a lesson hitting the
confirmation threshold becomes VALIDATED; demote stale/regime-mismatched rules aggressively). Run the
heavier **meta-reflection at the MONTH boundary** (1st 00:00 UTC) or when
`improvement` flags near-zero deployment / one-sided corpus / decaying returns. Present `report.json`:
actions, the balanced book + `neutral_check` (|net|/gross), equity, and the **monthly pacing read**
(on pace for ~3%/month? balanced? cost-positive rebalances? improving?).

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
