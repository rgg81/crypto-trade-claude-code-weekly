"""RESEARCH ONLY — not on the trading path, never imported by the desk.

Build a research dataset: funding-rate history + 4h klines for a WIDE USD-M perp universe.
Klines go through the local proxy (cached). Funding/exchangeInfo go direct (user-authorised).
Everything is cached to disk so re-runs cost zero calls."""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

SP = os.environ.get("TEMPEST_RESEARCH_DIR", os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(SP, "data")
FAPI = "https://fapi.binance.com"
PROXY = "http://127.0.0.1:8000"
PAUSE = 0.12          # deliberate throttle: the egress IP is shared with ~11 other desks


def _get(url, params=None, timeout=25):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "tempest-research/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def cached(name, fn):
    p = os.path.join(DATA, name)
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    v = fn()
    with open(p, "w") as f:
        json.dump(v, f)
    return v


def universe(top_n=60):
    def _fetch():
        info = _get(f"{FAPI}/fapi/v1/exchangeInfo")
        perp = {}
        for s in info["symbols"]:
            if (s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT"
                    and s.get("status") == "TRADING"):
                perp[s["symbol"]] = s.get("baseAsset", "")
        tick = _get(f"{FAPI}/fapi/v1/ticker/24hr")
        rows = [{"symbol": t["symbol"], "qv": float(t.get("quoteVolume") or 0.0),
                 "base": perp.get(t["symbol"], "")}
                for t in tick if t["symbol"] in perp]
        rows.sort(key=lambda r: -r["qv"])
        return rows
    return cached("universe.json", _fetch)[:top_n]


def funding(sym, start_ms, end_ms):
    """Full funding history in [start,end) — paginated, 1000 rows/call."""
    def _fetch():
        out, cur = [], start_ms
        while cur < end_ms:
            b = _get(f"{FAPI}/fapi/v1/fundingRate",
                     {"symbol": sym, "startTime": cur, "endTime": end_ms, "limit": 1000})
            if not b:
                break
            out.extend(b)
            nxt = int(b[-1]["fundingTime"]) + 1
            if nxt <= cur or len(b) < 1000:
                break
            cur = nxt
            time.sleep(PAUSE)
        return out
    return cached(f"fund_{sym}.json", _fetch)


def klines(sym, start_ms, end_ms, interval="4h"):
    def _fetch():
        out, cur = [], start_ms
        while cur < end_ms:
            b = _get(f"{PROXY}/fapi/v1/klines",
                     {"symbol": sym, "interval": interval, "startTime": cur,
                      "endTime": end_ms, "limit": 1500})
            if not b:
                break
            out.extend(b)
            nxt = int(b[-1][0]) + 1
            if nxt <= cur or len(b) < 1500:
                break
            cur = nxt
            time.sleep(PAUSE)
        return out
    return cached(f"kl_{sym}.json", _fetch)


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 365
    top = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    now = _get(f"{FAPI}/fapi/v1/time")["serverTime"]
    start = now - days * 86400_000
    uni = universe(top)
    print(f"universe: {len(uni)} symbols, {days}d window", flush=True)
    for i, u in enumerate(uni, 1):
        s = u["symbol"]
        try:
            f = funding(s, start, now)
            k = klines(s, start, now)
            print(f"  [{i}/{len(uni)}] {s:<14} funding {len(f):>5}  klines {len(k):>5}", flush=True)
        except Exception as e:
            print(f"  [{i}/{len(uni)}] {s:<14} ERROR {type(e).__name__}: {e}", flush=True)
            if "418" in str(e) or "-1003" in str(e) or "429" in str(e):
                print("  !! rate limited — STOPPING", flush=True)
                break
        time.sleep(PAUSE)
    print("done", flush=True)
