"""Deterministic, LLM-FREE driver for one TEMPEST-NEUTRAL 4h tick.

The all-weather desk is now a deterministic engine (blended_score + the gate), so a full cycle
needs NO model inference: scout -> preflight -> (deterministic news-neutral overlay) ->
blended_book_cli -> reclassify -> gate (once) -> post-gate neutrality guard. This script runs that
end-to-end so the loop survives Anthropic API outages — an OS cron fires it every ~30min regardless
of whether any LLM turn succeeds. The single-flight run lock + served-candle idempotency make it
safe to also run a manual `/loop` review concurrently; a SKIP tick is a cheap no-op.

Usage: uv run python scripts/auto_cycle.py     # runs the cycle if DUE, else prints a SKIP status
Exit codes: 0 ok (ran or skipped), 1 error.
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = [sys.executable]


def run(args, **kw):
    return subprocess.run(PY + args, cwd=ROOT, capture_output=True, text=True, **kw)


def _ccxt(sym: str) -> str:
    return sym if "/" in sym else sym[:-4] + "/USDT:USDT" if sym.endswith("USDT") else sym


def _held_symbols() -> list[str]:
    p = os.path.join(ROOT, "state", "positions.json")
    if not os.path.exists(p):
        return []
    return [_ccxt(x["symbol"]) for x in json.load(open(p))]


def _book():
    p = os.path.join(ROOT, "state", "positions.json")
    ps = json.load(open(p)) if os.path.exists(p) else []
    longs = [x["symbol"].replace("USDT", "") for x in ps if x["direction"] == "long"]
    shorts = [x["symbol"].replace("USDT", "") for x in ps if x["direction"] == "short"]
    return longs, shorts


def _gate_exposure(cycle: int):
    """Run the gate ONCE and return its parsed report dict (or None)."""
    r = run(["scripts/gate_execute_cli.py", "--cycle", str(cycle), "--loop", "strategic"])
    txt = r.stdout
    i = txt.rfind('{\n  "cycle"')
    if i < 0:
        i = txt.find("{")
    try:
        return json.loads(txt[i:])
    except Exception:  # noqa: BLE001
        print("GATE raw output:\n", txt[-1500:], r.stderr[-500:])
        return None


def main() -> int:
    rl = run(["scripts/run_loops.py"])
    try:
        st = json.loads(rl.stdout)["strategic"]
    except Exception:  # noqa: BLE001
        print("run_loops failed:", rl.stdout[-400:], rl.stderr[-400:])
        return 1
    cycle = st.get("cycle")
    if not st.get("due"):
        longs, shorts = _book()
        flat = not longs or not shorts
        print(f"SKIP cycle {cycle} | {'FLAT!' if flat else 'deployed'} | "
              f"LONG {'/'.join(longs)} vs SHORT {'/'.join(shorts)}")
        return 0

    cdir = os.path.join(ROOT, "state", "cycle", str(cycle))
    print(f"DUE cycle {cycle}: running deterministic blended tick")

    run(["scripts/scout_cli.py", "--cycle", str(cycle), "--top", "12"])
    uni = json.load(open(os.path.join(cdir, "universe.json")))
    uni_syms = [s["symbol"] for s in uni.get("universe", uni.get("candidates", []))]
    symbols = list(dict.fromkeys(uni_syms + _held_symbols()))  # union, order-preserving
    pf = run(["scripts/preflight.py", "--cycle", str(cycle), "--symbols", ",".join(symbols)])
    if not os.path.exists(os.path.join(cdir, "context.json")):
        print("preflight failed:", pf.stdout[-400:], pf.stderr[-600:])
        return 1

    # deterministic news-neutral overlay (regime engine flags risk_off independently; blended engine
    # excludes pumps deterministically) -> satisfies the gate funnel + reclassify without any LLM.
    raw = [s.split("/")[0] + "USDT" for s in symbols]
    reps = [{"agent": "news", "symbol": s, "stance": "neutral", "confidence": 0.3,
             "key_points": ["Deterministic auto-cycle: no LLM news read; regime engine sets risk, "
                            "blended engine excludes pumps."],
             "signals": {"catalyst_count": 0, "risk_off_flag": 0}} for s in raw]
    json.dump(reps, open(os.path.join(cdir, "analyst_reports.json"), "w"), indent=2)

    bb = run(["scripts/blended_book_cli.py", "--cycle", str(cycle)])
    if not os.path.exists(os.path.join(cdir, "proposals.json")):
        print("blended_book_cli failed:", bb.stdout[-400:], bb.stderr[-600:])
        return 1
    try:
        plan = json.loads(bb.stdout)["plan"]
        nrot = len(plan["close"]) + len(plan["open_long"]) + len(plan["open_short"])
        print(f"plan: keep L{plan['keep_long']} S{plan['keep_short']} | open L{plan['open_long']} "
              f"S{plan['open_short']} | close {plan['close']} | rot {nrot}")
    except Exception:  # noqa: BLE001
        pass

    run(["scripts/reclassify_cli.py", "--cycle", str(cycle)])

    rep = _gate_exposure(cycle)
    if rep is None:
        return 1
    e = rep["exposure"]
    print(f"gate: opened {rep['opened']} closed {rep['closed']} reduced {rep['reduced']} | "
          f"net ${e['net']:+.0f} tilt {e['tilt']:.4f} L{e['n_long']}/S{e['n_short']} "
          f"equity {rep['equity']:.2f} halt {rep['halted']}")

    # POST-GATE NEUTRALITY GUARD: a rotation into an asymmetric held book can leave it imbalanced.
    if abs(e["tilt"]) > 0.03 or e["n_long"] != e["n_short"]:
        gl, gs = e["gross_long"], e["gross_short"]
        big = "short" if gs > gl else "long"
        frac = round(abs(gs - gl) / max(gs, gl, 1e-9), 4)
        ps = json.load(open(os.path.join(ROOT, "state", "positions.json")))
        mgmt = []
        for x in ps:
            if x["direction"] == big and frac > 0:
                mgmt.append({"symbol": x["symbol"], "action": "reduce", "reduce_fraction": frac,
                             "note": "auto neutrality guard — trim oversized sleeve to neutral."})
            else:
                mgmt.append({"symbol": x["symbol"], "action": "hold", "note": "guard hold."})
        json.dump({"proposals": [], "management": mgmt, "triggers": [], "cancel_triggers": []},
                  open(os.path.join(cdir, "proposals.json"), "w"), indent=2)
        print(f"NEUTRALITY GUARD: tilt {e['tilt']:.3f} -> trimming {big} sleeve by {frac}")
        rep2 = _gate_exposure(cycle)
        if rep2:
            e2 = rep2["exposure"]
            print(f"  after guard: net ${e2['net']:+.0f} tilt {e2['tilt']:.4f} "
                  f"L{e2['n_long']}/S{e2['n_short']}")

    longs, shorts = _book()
    flat = not longs or not shorts
    print(f"SUMMARY cycle {cycle} | {'FLAT! (VIOLATION)' if flat else 'deployed'} | "
          f"LONG {'/'.join(longs)} vs SHORT {'/'.join(shorts)} | equity {rep['equity']:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
