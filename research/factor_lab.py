"""RESEARCH ONLY — not on the trading path, never imported by the desk.

Carry-harvester and factor research on the WIDE dataset.

Design notes that matter:
 - Panel is aligned to 4h bars. Funding settles on each symbol's OWN schedule (1h/4h/8h); a bar
   credits every settlement whose fundingTime falls inside it, so a 1h-interval name accrues 4x
   an 8h name over the same bar. Getting this wrong invents carry.
 - Entry/exit at the bar CLOSE, forward return close[t]->close[t+1]. Funding earned over that bar
   is known at t (Binance publishes the rate in advance), so using it is not lookahead.
 - t = Sharpe_annual * sqrt(n/2190). With ~2190 bars, t ~= Sharpe. Report BOTH.
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


def load_panel(min_bars=1500):
    """symbol -> {ts: {'px':close,'fund':rate_credited_in_this_bar,'vol':...}} on a common grid."""
    panel, syms = {}, []
    for p in sorted(glob.glob(os.path.join(DATA, "kl_*.json"))):
        sym = os.path.basename(p)[3:-5]
        fp = os.path.join(DATA, f"fund_{sym}.json")
        if not os.path.exists(fp):
            continue
        kl = json.load(open(p))
        fu = json.load(open(fp))
        if len(kl) < min_bars:
            continue
        bars = {}
        for row in kl:
            ts = int(row[0]) - (int(row[0]) % BAR_MS)
            bars[ts] = {"px": float(row[4]), "fund": 0.0, "n": 0}
        for f in fu:
            ts = int(f["fundingTime"])
            b = ts - (ts % BAR_MS)
            if b in bars:
                bars[b]["fund"] += float(f["fundingRate"])
                bars[b]["n"] += 1
        panel[sym] = bars
        syms.append(sym)
    grid = sorted(set().union(*[set(v) for v in panel.values()])) if panel else []
    return panel, syms, grid


def ann_t(sharpe, n):
    return sharpe * math.sqrt(n / CYC_YR)


def stats(rets, label="", fees=0.0, opens=0):
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
    return {"label": label, "n": n, "sharpe": sh, "t": ann_t(sh, n),
            "total_pct": (eq - 1) * 100, "mdd_pct": mdd * 100,
            "permonth_pct": ((eq ** ((30 * 24 / 4) / n)) - 1) * 100,
            "fees": fees, "opens": opens}


def show(s):
    print(f"  {s['label']:<32} sharpe {s['sharpe']:>6.2f} (t {s['t']:>5.2f}) "
          f"| /mo {s['permonth_pct']:>6.2f}% | mdd {s['mdd_pct']:>5.1f}% "
          f"| fees ${s['fees']:>7.0f} | opens {s['opens']:>5} | n {s['n']}")


def build(panel, syms, grid):
    """Per-bar view. SIGNAL = last SETTLED funding at or before t (no lookahead: the rate that
    will settle next is not known exactly at t). EARN = funding actually settled inside bar t+1."""
    last = {s: None for s in syms}
    rows = []
    for ts in grid:
        cur = {}
        for s in syms:
            b = panel[s].get(ts)
            if b is None:
                continue
            if b["n"]:
                last[s] = b["fund"] / b["n"]          # per-settlement rate, latest known
            if last[s] is None:
                continue
            cur[s] = {"px": b["px"], "sig": last[s], "earn": b["fund"]}
        rows.append((ts, cur))
    return rows


def run_carry(rows, *, n_side=3, band=0.0, fee=0.0007, gross=10_000.0, use_signal=True,
              universe_cap=None):
    """LONG the most-negative-funding names (we get paid), SHORT the most-positive. Dollar-neutral.
    `band` = hysteresis in signal units; a held leg is kept until it falls `band` past the
    cutoff."""
    leg = gross / (2 * n_side)
    held, rets, opens, closes, fees_paid = {}, [], 0, 0, 0.0
    for i in range(len(rows) - 1):
        _, cur = rows[i]
        _, nxt = rows[i + 1]
        elig = [s for s in cur if s in nxt]
        if universe_cap:
            elig = elig[:universe_cap]
        if len(elig) < 2 * n_side:
            rets.append(0.0)
            continue
        key = (lambda s, _c=cur: _c[s]["sig"]) if use_signal else (lambda s: s)
        elig.sort(key=key)
        want_l, want_s = elig[:n_side], elig[-n_side:]
        lo_cut, hi_cut = cur[want_l[-1]]["sig"], cur[want_s[0]]["sig"]
        new = {}
        for s, d in held.items():
            if s not in cur or s not in nxt:
                continue
            if d == "long" and cur[s]["sig"] <= lo_cut + band:
                new[s] = d
            elif d == "short" and cur[s]["sig"] >= hi_cut - band:
                new[s] = d
        for s in want_l:
            if sum(1 for v in new.values() if v == "long") >= n_side:
                break
            new.setdefault(s, "long")
        for s in want_s:
            if sum(1 for v in new.values() if v == "short") >= n_side:
                break
            new.setdefault(s, "short")
        # keep sides balanced
        nl = [s for s, d in new.items() if d == "long"]
        ns = [s for s, d in new.items() if d == "short"]
        k = min(len(nl), len(ns))
        new = {s: "long" for s in nl[:k]} | {s: "short" for s in ns[:k]}
        o = sum(1 for s in new if held.get(s) != new[s])
        c = sum(1 for s in held if new.get(s) != held[s])
        opens += o
        closes += c
        f = (o + c) * leg * fee
        fees_paid += f
        pnl = 0.0
        for s, d in new.items():
            r = nxt[s]["px"] / cur[s]["px"] - 1.0
            pnl += (r if d == "long" else -r) * leg
            pnl += (-1.0 if d == "long" else 1.0) * nxt[s]["earn"] * leg
        rets.append((pnl - f) / gross)
        held = new
    return rets, fees_paid, opens


def vol_of(panel, sym, ts, lookback=30):
    """Realised 4h vol over the trailing `lookback` bars ending at ts (no lookahead)."""
    bars = panel[sym]
    keys = [k for k in sorted(bars) if k <= ts][-(lookback + 1):]
    if len(keys) < 8:
        return None
    px = [bars[k]["px"] for k in keys]
    rs = [px[i + 1] / px[i] - 1.0 for i in range(len(px) - 1)]
    sd = statistics.pstdev(rs)
    return sd if sd > 1e-6 else None


def run_v2(rows, panel, *, n_side=3, band=0.0, fee=0.0007, gross=10_000.0,
           allowed=None, inv_vol=False, mom_filter=None, signal="carry", vol_lb=30):
    """Dollar-neutral by construction (gross long $ == gross short $) but leg sizes may differ:
    with inv_vol=True legs are inverse-volatility weighted INSIDE each sleeve, which keeps the
    dollar-neutral mandate while stopping one violent small cap from dominating the book.

    mom_filter: exclude a name from the SHORT sleeve if its trailing return over `mom_filter`
    bars exceeds +threshold (never short a ripping name), and from LONG if it is crashing.
    """
    held, rets, opens, fees_paid = {}, [], 0, 0.0
    prev_notional = {}
    for i in range(len(rows) - 1):
        ts, cur = rows[i]
        _, nxt = rows[i + 1]
        elig = [s for s in cur if s in nxt and (allowed is None or s in allowed)]
        if mom_filter:
            lb, thr = mom_filter
            keep = []
            for s in elig:
                bars = panel[s]
                ks = [k for k in sorted(bars) if k <= ts][-(lb + 1):]
                if len(ks) < lb:
                    keep.append(s)
                    continue
                m = bars[ks[-1]]["px"] / bars[ks[0]]["px"] - 1.0
                cur[s]["mom"] = m
                keep.append(s)
            elig = keep
        if len(elig) < 2 * n_side:
            rets.append(0.0)
            continue
        key = {"carry": lambda s, _c=cur: _c[s]["sig"],
               "none": lambda s: s}[signal]
        elig.sort(key=key)
        cand_l = [s for s in elig if not (mom_filter and cur[s].get("mom", 0) < -mom_filter[1])]
        cand_s = [s for s in reversed(elig)
                  if not (mom_filter and cur[s].get("mom", 0) > mom_filter[1])]
        want_l, want_s = cand_l[:n_side], cand_s[:n_side]
        if len(want_l) < n_side or len(want_s) < n_side:
            rets.append(0.0)
            continue
        lo_cut = cur[want_l[-1]]["sig"]
        hi_cut = cur[want_s[-1]]["sig"]
        new = {}
        for s, d in held.items():
            if s not in cur or s not in nxt:
                continue
            if d == "long" and cur[s]["sig"] <= lo_cut + band:
                new[s] = d
            elif d == "short" and cur[s]["sig"] >= hi_cut - band:
                new[s] = d
        for s in want_l:
            if sum(1 for v in new.values() if v == "long") >= n_side:
                break
            new.setdefault(s, "long")
        for s in want_s:
            if sum(1 for v in new.values() if v == "short") >= n_side:
                break
            new.setdefault(s, "short")
        nl = [s for s, d in new.items() if d == "long"]
        ns_ = [s for s, d in new.items() if d == "short"]
        k = min(len(nl), len(ns_))
        if k == 0:
            rets.append(0.0)
            held = {}
            continue
        nl, ns_ = nl[:k], ns_[:k]
        # sizing: equal-$ or inverse-vol, each sleeve summing to gross/2 (=> dollar-neutral)
        def sizes(side, _ts=ts):
            if not inv_vol:
                return {s: gross / 2 / len(side) for s in side}
            w = {}
            for s in side:
                v = vol_of(panel, s, _ts, vol_lb)
                w[s] = 1.0 / v if v else 0.0
            tot = sum(w.values()) or 1.0
            return {s: gross / 2 * w[s] / tot for s in side}
        notional = sizes(nl) | sizes(ns_)
        new = {s: "long" for s in nl} | {s: "short" for s in ns_}
        traded = sum(abs(notional.get(s, 0) - prev_notional.get(s, 0))
                     for s in set(notional) | set(prev_notional))
        opens += sum(1 for s in new if held.get(s) != new[s])
        f = traded * fee
        fees_paid += f
        pnl = 0.0
        for s, d in new.items():
            leg = notional[s]
            r = nxt[s]["px"] / cur[s]["px"] - 1.0
            pnl += (r if d == "long" else -r) * leg
            pnl += (-1.0 if d == "long" else 1.0) * nxt[s]["earn"] * leg
        rets.append((pnl - f) / gross)
        held = new
        prev_notional = notional
    return rets, fees_paid, opens
