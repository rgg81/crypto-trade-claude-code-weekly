# research/ — offline factor research (NOT on the trading path)

Nothing here is imported by the desk. `fetch_panel.py` builds a cached panel (funding-rate history
direct from Binance, 4h klines via the local proxy); `factor_lab.py` evaluates signals and
portfolios on it. Everything is cached to disk, so re-runs cost zero API calls.

    TEMPEST_RESEARCH_DIR=/some/scratch uv run python research/fetch_panel.py 365 60

## Findings, 2026-08-28 (1 year x 60 symbols x 2190 4h bars)

**Read these before proposing a new edge for this desk.** NOTE: findings 1-5 below were all measured
on TOP-N/BOTTOM-N books and are superseded in scope by the breadth section further down — the "no
edge" conclusion was an artifact of concentration, not an absence of signal.

1. **The desk has no price alpha at 4h.** Rank-IC of every traded signal vs the next-cycle return
   over the live 353-cycle record: momentum +0.0018 (t +0.08), carry -0.0024 (t -0.12),
   mean-reversion +0.0013 (t +0.06), blend +0.0010 (t +0.05). No configuration is positive-Sharpe
   in both halves, including a zero-fee upper bound.

2. **The live desk IS genuinely market-neutral — this part works.** Realised beta to its own
   universe **+0.0159** (corr +0.103, n=355); only **1.1%** of book variance is market beta; book
   vol 0.39%/cycle vs market 2.54%/cycle. The dollar-neutral construction, momentum-consistency
   gate and liquid-majors filter are doing their job.

3. **Carry harvesting on a wide universe FAILS.** Long most-negative / short most-positive funding
   over 54 names: Sharpe -0.6 to -3.7 at every setting, max drawdown 32-93%. High funding marks a
   name that is squeezing; shorting it is short-gamma and the right tail eats years of carry. Adding
   inverse-vol sizing and a never-short-a-ripper filter does not rescue it, and results get
   MONOTONICALLY WORSE as the universe widens (top20 +1.06 -> top40 -1.23 -> all54 -1.99), which is
   the opposite of what a real carry edge would do.

4. **Positive IC does NOT imply a profitable long/short book.** The low-vol factor has IC t=+8.48
   with near-identical halves, yet the L/S portfolio returns Sharpe **-2.84**. IC measures the
   middle of the cross-section; the portfolio trades the extremes, where crypto's fat right tail
   lives. ALWAYS build the portfolio before believing an IC.

5. **Every apparent edge here is short-alt-beta in disguise.** Low-vol beta -0.824, reversal -0.293,
   an OI/"quality" factor -0.420 that held BTC/ETH/SOL in 353/353 cycles. They make money only in
   down tape, so they fail an all-weather mandate regardless of backtest Sharpe.

6. **Cash-and-carry (long spot / short perp) is the one real neutral yield** — but small: ~+3%/yr
   gross on liquid majors (BTC +3.33%, LINK +4.69%, UNI +4.46%, ETH +2.38%; BTC funding positive in
   76% of settlements). Median across the top 25 is +1.93%/yr, equal-weight mean -0.89%/yr (dragged
   by outliers like ONG at -90%/yr). Requires SPOT legs, which this futures-only desk does not have.

## 2026-08-28 (later) — THE EARLIER "NO EDGE" CONCLUSION WAS WRONG. Breadth was the problem.

Everything above tested top-3/bottom-3 out of a small universe. That is not a quant book, it is a
concentrated punt on the two most extreme names — which in crypto are the most volatile. It is
exactly why signals with clearly positive IC produced losing portfolios: the book only ever
harvested the signal where the fat tails live. Rebuilt properly (`xsection_lab.py`) on 230 perps x
2190 4h bars, with:

  * a SMALL weight in EVERY name, proportional to cross-sectional z-score (not top-N),
  * inverse-volatility leg scaling,
  * ITERATED beta-neutralisation PLUS an explicit market hedge (one pass is not enough — de-meaning
    after the projection re-introduces beta; realised beta only fell -0.271 -> -0.052 with both),
  * a per-name weight cap and a turnover cap.

**HEADLINE (top-100 by volume, restricted to names listed the FULL year = 87 names; momentum over
150 bars / 25 days; beta-hedged; turnover cap 2% of gross per bar):**

    sharpe 2.48 (t 2.48) | +3.86%/month | max drawdown 10.2% | fees $285 | turnover 41x
    split-half   H1 +2.98   H2 +2.10
    realised beta -0.047 | UP tape -0.000%/bar | DOWN tape +0.046%/bar
    quarters     Q1 +18.1%  Q2 +8.8%  Q3 +2.2%  Q4 +20.8%   (all four positive)
    fees         .05% -> 2.52   .07% -> 2.48   .12% -> 2.38   .20% -> 2.21

**BREADTH IS THE WHOLE STORY** — same signal, only the leg count changes:

    legs/side    3      5      8      12     20     30     87
    sharpe      1.19   1.57   1.75   1.90   1.96   2.25   2.48
    max DD     35.3%  26.1%  21.4%  18.5%  13.8%  11.0%  10.2%

`IR = IC*sqrt(breadth)`, measured. Note the desk's CURRENT 3-leg structure carries a 35% drawdown —
it would trip the -15% force-flatten repeatedly. Concentration, not signal, was the defect.
Minimum viable is ~30 legs/side (60 legs) to keep drawdown clear of the flatten.

Robustness: the lookback is a broad plateau (mom30 1.93, mom90 1.94, mom150 1.63, mom400 1.98), not a
knife edge; both the lookback and turnover-cap sweeps are MONOTONE; and the headline is measured with
mid-year listings EXCLUDED, which is the main survivorship channel.

Caveats to respect: one year, one regime; 87 names is a small effective sample for a factor claim;
t=2.48 is real but not overwhelming; and this needs ~60 legs, which the current gate (built for 3-6
legs with per-trade stops and a heat cap) cannot express without a sizing redesign.

## Statistical guardrails learned the hard way

- On n 4h bars, **t = Sharpe_annual * sqrt(n/2190)**. On the desk's 353-cycle record that is
  `t = Sharpe * 0.401`, so an annualised Sharpe of 3.24 is only t=1.30 — NOT significant. Convert
  before believing anything.
- **Count the opens.** A "strategy" with 2-14 opens over hundreds of bars is a single position held
  for the whole sample (~1 independent bet), not a signal. Several such books showed "+9%, Sharpe
  2.7" and were pure artifact.
- **Check beta and the up/down-tape split** on every candidate factor, not just total return.
