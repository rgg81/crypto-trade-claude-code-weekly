"""RESEARCH ONLY — not on the trading path, never imported by the desk.

Proper cross-sectional portfolio construction.

What was wrong before: top-N/bottom-N out of a small universe concentrates the book in the two
most extreme names, which in crypto are the most volatile. That is why a signal with clearly
positive IC still produced a losing L/S book — the tails ate it. A real book instead:
  * takes a SMALL weight in EVERY name, proportional to cross-sectional signal rank/z,
  * scales each leg by 1/vol so no single name dominates risk,
  * NEUTRALISES beta explicitly (dollar-neutral != beta-neutral),
  * caps per-name weight,
  * and trades the whole cross-section, so breadth actually works for it (IR ~ IC*sqrt(breadth)).
"""
import glob
import json
import math
import os
import statistics

SP = os.environ.get("TEMPEST_RESEARCH_DIR", os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(SP, "data")
BAR_MS = 4 * 3600 * 1000
CYC_YR = 365 * 24 / 4


def load(min_bars=1500):
    panel = {}
    for p in sorted(glob.glob(os.path.join(DATA, "kl_*.json"))):
        sym = os.path.basename(p)[3:-5]
        fp = os.path.join(DATA, f"fund_{sym}.json")
        kl = json.load(open(p))
        if len(kl) < min_bars:
            continue
        fu = json.load(open(fp)) if os.path.exists(fp) else []
        bars = {}
        for row in kl:
            ts = int(row[0]) - (int(row[0]) % BAR_MS)
            bars[ts] = {"px": float(row[4]), "fund": 0.0, "n": 0,
                        "qv": float(row[7]) if len(row) > 7 else 0.0}
        for f in fu:
            ts = int(f["fundingTime"])
            b = ts - (ts % BAR_MS)
            if b in bars:
                bars[b]["fund"] += float(f["fundingRate"])
                bars[b]["n"] += 1
        panel[sym] = bars
    grid = sorted(set().union(*[set(v) for v in panel.values()])) if panel else []
    return panel, grid


def series(panel):
    idx = {s: sorted(panel[s]) for s in panel}
    pos = {s: {t: i for i, t in enumerate(idx[s])} for s in panel}
    return idx, pos


def zmap(d, clip=3.0):
    v = [x for x in d.values() if x is not None and not math.isnan(x)]
    if len(v) < 8:
        return {}
    m = statistics.mean(v)
    sd = statistics.pstdev(v)
    if sd < 1e-12:
        return {}
    out = {}
    for k, x in d.items():
        if x is None or math.isnan(x):
            continue
        out[k] = max(-clip, min(clip, (x - m) / sd))
    return out


def demean(w):
    if not w:
        return w
    m = sum(w.values()) / len(w)
    return {k: v - m for k, v in w.items()}


def beta_neutralise(w, betas):
    """Remove the portfolio's net beta by subtracting a beta-proportional offset."""
    if not w:
        return w
    b = {k: betas.get(k, 1.0) for k in w}
    num = sum(w[k] * b[k] for k in w)
    den = sum(b[k] * b[k] for k in w) or 1e-9
    lam = num / den
    return {k: w[k] - lam * b[k] for k in w}


def normalise(w, gross):
    tot = sum(abs(v) for v in w.values())
    if tot < 1e-12:
        return {}
    return {k: v / tot * gross for k, v in w.items()}


def cap_weights(w, gross, max_frac):
    cap = gross * max_frac
    return {k: max(-cap, min(cap, v)) for k, v in w.items()}


def build_features(panel, idx, pos, grid, lookbacks=(6, 30, 90), vol_lb=30, beta_lb=180):
    """Per-bar feature dict. All lookbacks END at t (no lookahead)."""
    feats = {}
    last_fund = {s: None for s in panel}
    # rolling market return for beta
    mkt = {}
    for t in grid:
        rs = []
        for s in panel:
            i = pos[s].get(t)
            if i is None or i < 1:
                continue
            a = panel[s][idx[s][i - 1]]["px"]
            b = panel[s][idx[s][i]]["px"]
            if a > 0:
                rs.append(b / a - 1.0)
        mkt[t] = statistics.mean(rs) if len(rs) >= 5 else 0.0
    for t in grid:
        row = {}
        for s in panel:
            i = pos[s].get(t)
            if i is None:
                continue
            b = panel[s][t]
            if b["n"]:
                last_fund[s] = b["fund"] / b["n"]
            if i < max(lookbacks) + 2 or last_fund[s] is None:
                continue
            px = panel[s][t]["px"]
            f = {"px": px, "fund": last_fund[s], "earn": b["fund"], "qv": b["qv"]}
            for lb in lookbacks:
                p0 = panel[s][idx[s][i - lb]]["px"]
                f[f"mom{lb}"] = px / p0 - 1.0 if p0 > 0 else None
            w = [panel[s][idx[s][j]]["px"] for j in range(max(0, i - vol_lb), i + 1)]
            rr = [w[k + 1] / w[k] - 1.0 for k in range(len(w) - 1) if w[k] > 0]
            f["vol"] = statistics.pstdev(rr) if len(rr) > 8 else None
            # rolling beta to the equal-weight market
            j0 = max(1, i - beta_lb)
            xs, ys = [], []
            for j in range(j0, i + 1):
                tj = idx[s][j]
                pa = panel[s][idx[s][j - 1]]["px"]
                pb = panel[s][tj]["px"]
                if pa > 0 and tj in mkt:
                    ys.append(pb / pa - 1.0)
                    xs.append(mkt[tj])
            if len(xs) > 30:
                mx = statistics.mean(xs)
                my = statistics.mean(ys)
                cov = sum((a - mx) * (bb - my) for a, bb in zip(xs, ys, strict=False)) / len(xs)
                var = statistics.pvariance(xs)
                f["beta"] = cov / var if var > 1e-14 else 1.0
            else:
                f["beta"] = 1.0
            row[s] = f
        feats[t] = row
    return feats, mkt


def backtest(feats, grid, signal_fn, *, gross=10_000.0, fee=0.0007, max_frac=0.05,
             inv_vol=True, beta_neutral=True, min_names=20, turnover_cap=None):
    """signal_fn(row) -> {sym: raw_score}
    positive = long."""
    prev, rets, turn, fees_tot = {}, [], 0.0, 0.0
    for i in range(len(grid) - 1):
        t, tn = grid[i], grid[i + 1]
        row = feats.get(t, {})
        nxt = feats.get(tn, {})
        live = {s: f for s, f in row.items() if s in nxt and f.get("vol")}
        if len(live) < min_names:
            rets.append(0.0)
            continue
        raw = signal_fn(live)
        z = zmap(raw)
        if len(z) < min_names:
            rets.append(0.0)
            continue
        w = demean(z)
        if inv_vol:
            w = {s: v / max(live[s]["vol"], 1e-4) for s, v in w.items()}
            w = demean(w)
        if beta_neutral:
            w = beta_neutralise(w, {s: live[s].get("beta", 1.0) for s in w})
            w = demean(w)
        w = normalise(w, gross)
        w = cap_weights(w, gross, max_frac)
        w = normalise(w, gross)
        traded = sum(abs(w.get(s, 0.0) - prev.get(s, 0.0)) for s in set(w) | set(prev))
        if turnover_cap and traded > gross * turnover_cap:
            scale = gross * turnover_cap / traded
            w = {s: prev.get(s, 0.0) + (w.get(s, 0.0) - prev.get(s, 0.0)) * scale
                 for s in set(w) | set(prev)}
            traded = sum(abs(w.get(s, 0.0) - prev.get(s, 0.0)) for s in set(w) | set(prev))
        cost = traded * fee
        turn += traded
        fees_tot += cost
        pnl = 0.0
        for s, notional in w.items():
            if s not in nxt or s not in row:
                continue
            r = nxt[s]["px"] / row[s]["px"] - 1.0
            pnl += notional * r
            pnl -= notional * nxt[s]["earn"]       # long pays when funding>0
        rets.append((pnl - cost) / gross)
        prev = w
    return rets, fees_tot, turn


def stats(rets, label="", fees=0.0, turn=0.0, gross=10_000.0):
    n = len(rets)
    m = statistics.mean(rets)
    sd = statistics.pstdev(rets) or 1e-12
    sh = m / sd * math.sqrt(CYC_YR)
    eq = 1.0
    peak = 1.0
    mdd = 0.0
    for r in rets:
        eq *= (1 + r)
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)
    return {"label": label, "n": n, "sharpe": sh, "t": sh * math.sqrt(n / CYC_YR),
            "permonth": ((eq ** ((30 * 24 / 4) / n)) - 1) * 100 if n else 0.0,
            "mdd": mdd * 100, "fees": fees, "turn_x": turn / gross}


def show(s):
    print(f"  {s['label']:<34} sharpe {s['sharpe']:>6.2f} (t {s['t']:>5.2f}) "
          f"| /mo {s['permonth']:>6.2f}% | mdd {s['mdd']:>5.1f}% "
          f"| fees ${s['fees']:>8.0f} | turn {s['turn_x']:>6.0f}x")


def backtest2(feats, grid, signal_fn, *, gross=10_000.0, fee=0.0007, max_frac=0.05,
              inv_vol=True, beta_neutral=True, min_names=20, turnover_cap=None,
              allowed=None, beta_iters=3, hedge_beta=False, mkt=None, n_side_limit=None):
    """As backtest(), plus: universe restriction, ITERATED beta-neutralisation (one pass leaves
    residual beta because de-meaning after the projection re-introduces it), and an optional
    explicit market hedge that removes whatever beta survives."""
    prev, rets, turn, fees_tot, betas_real = {}, [], 0.0, 0.0, []
    for i in range(len(grid) - 1):
        t, tn = grid[i], grid[i + 1]
        row = feats.get(t, {})
        nxt = feats.get(tn, {})
        live = {s: f for s, f in row.items()
                if s in nxt and f.get("vol") and (allowed is None or s in allowed)}
        if len(live) < min_names:
            rets.append(0.0)
            continue
        z = zmap(signal_fn(live))
        if len(z) < min_names:
            rets.append(0.0)
            continue
        w = demean(z)
        if inv_vol:
            w = demean({s: v / max(live[s]["vol"], 1e-4) for s, v in w.items()})
        if beta_neutral:
            b = {s: live[s].get("beta", 1.0) for s in w}
            for _ in range(max(1, beta_iters)):
                w = demean(beta_neutralise(w, b))
        w = normalise(cap_weights(normalise(w, gross), gross, max_frac), gross)
        if n_side_limit:
            w = normalise(top_n_mask(w, n_side_limit), gross)
        bexp = sum(w[s] * live[s].get("beta", 1.0) for s in w) / gross
        betas_real.append(bexp)
        traded = sum(abs(w.get(s, 0.0) - prev.get(s, 0.0)) for s in set(w) | set(prev))
        if turnover_cap and traded > gross * turnover_cap:
            sc = gross * turnover_cap / traded
            w = {s: prev.get(s, 0.0) + (w.get(s, 0.0) - prev.get(s, 0.0)) * sc
                 for s in set(w) | set(prev)}
            traded = sum(abs(w.get(s, 0.0) - prev.get(s, 0.0)) for s in set(w) | set(prev))
        cost = traded * fee
        turn += traded
        fees_tot += cost
        pnl = 0.0
        for s, notional in w.items():
            if s not in nxt or s not in row:
                continue
            pnl += notional * (nxt[s]["px"] / row[s]["px"] - 1.0)
            pnl -= notional * nxt[s]["earn"]
        if hedge_beta and mkt is not None:
            pnl -= bexp * gross * mkt.get(tn, 0.0)
        rets.append((pnl - cost) / gross)
        prev = w
    return rets, fees_tot, turn, betas_real


def top_n_mask(w, n_side):
    """Keep only the n_side largest longs and n_side largest shorts — the implementable book."""
    if not w:
        return w
    longs = sorted([(v, k) for k, v in w.items() if v > 0], reverse=True)[:n_side]
    shorts = sorted([(v, k) for k, v in w.items() if v < 0])[:n_side]
    return {k: v for v, k in longs + shorts}
