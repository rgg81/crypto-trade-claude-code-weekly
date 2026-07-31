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
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = [sys.executable]

# Binance -1003 IP-ban message carries a `banned until <epoch_ms>` deadline. Every REST fetch made
# while the ban is active re-extends it ~22min, so we persist the deadline and HOLD before touching
# the exchange until it lapses (else the ban ratchets ahead of real time — observed cy190).
_BAN_RE = re.compile(r"banned until (\d{10,})")


def _parse_ban_until_ms(text: str) -> int | None:
    """Extract the `banned until <epoch_ms>` deadline from a -1003 message, or None if absent."""
    m = _BAN_RE.search(text or "")
    return int(m.group(1)) if m else None


def _ban_path(state_dir) -> str:
    return os.path.join(str(state_dir), "ban.json")


def _record_ban(state_dir, banned_until_ms: int) -> None:
    """Persist the ban deadline, keeping the LATEST (max) — a later ban extends, an earlier never
    shortens. Best-effort: a write failure must never break the hold path."""
    try:
        prev = 0
        p = _ban_path(state_dir)
        if os.path.exists(p):
            prev = int(json.load(open(p)).get("banned_until_ms", 0))
        deadline = max(prev, int(banned_until_ms))
        json.dump({"banned_until_ms": deadline}, open(p, "w"))
    except Exception:  # noqa: BLE001 — telemetry only; never break the driver
        pass


def _ban_remaining_ms(state_dir, now_ms: int | None = None) -> int:
    """Milliseconds until the recorded ban lapses (0 if none / already lapsed -> safe to fetch)."""
    p = _ban_path(state_dir)
    if not os.path.exists(p):
        return 0
    try:
        deadline = int(json.load(open(p)).get("banned_until_ms", 0))
    except Exception:  # noqa: BLE001
        return 0
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    return max(0, deadline - now_ms)


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


_REVIEW_MIN_DAYS = 30


def _last_review_ts(review_dir) -> datetime | None:
    """When the monthly review LAST RAN — None if it never has.

    Keyed off the `run_ts` stamped inside each report, falling back to the file mtime. NOT off the
    filename: a report is named for the month it COVERS (`YYYY-MM.json`) and is rewritten in place
    on a re-run, so the name never advances. Reading the date from it pinned "last review" to the
    1st of the month, which made the gate re-fire on EVERY 30-min tick from the 31st onward (cy216).
    """
    try:
        names = sorted(os.listdir(review_dir))
    except OSError:                                  # no review dir yet -> never run
        return None
    latest = None
    for f in names:
        if not f.endswith(".json"):
            continue
        p = os.path.join(review_dir, f)
        ts = None
        try:
            ts = datetime.fromisoformat(json.load(open(p))["run_ts"])
            if ts.tzinfo is not None:                # normalize: never mix aware/naive datetimes
                ts = ts.astimezone().replace(tzinfo=None)
        except Exception:  # noqa: BLE001 — legacy/partial report -> fall back to the file mtime
            try:
                ts = datetime.fromtimestamp(os.path.getmtime(p))
            except OSError:
                continue
        if latest is None or ts > latest:
            latest = ts
    return latest


def _monthly_review_due(review_dir, now: datetime | None = None,
                        min_days: int = _REVIEW_MIN_DAYS) -> bool:
    """True when the blended-score parameter review is due (never run, or >= min_days ago)."""
    last = _last_review_ts(review_dir)
    if last is None:
        return True
    return ((now or datetime.now()) - last) >= timedelta(days=min_days)


def _review_summary_line(report: dict) -> str:
    """One-line verdict from a review report — the loop only ever shows this."""
    perf = report.get("performance") or {}
    neut = report.get("neutrality") or {}
    recs = report.get("recommendations") or []
    mo = perf.get("monthly_return_pct")
    dd = perf.get("max_drawdown_pct")
    parts = [f"{mo:+.2f}%/mo" if mo is not None else "n/a %/mo",
             f"maxDD {dd:.2f}%" if dd is not None else "maxDD n/a",
             f"{neut.get('neutrality_violations', '?')} neutrality violations",
             f"{perf.get('cycles_analyzed', '?')} cycles"]
    tail = "none" if not recs else ", ".join(str(r.get("type", "?")) for r in recs)
    return " | ".join(parts) + f" | recs: {tail}"


def _check_monthly_review() -> bool:
    """Run the blended-score parameter review if it is due. True if it ran."""
    review_dir = os.path.join(ROOT, "state", "monthly_review")
    last = _last_review_ts(review_dir)
    if not _monthly_review_due(review_dir):
        return False

    ago = f"{(datetime.now() - last).days}d ago" if last else "never run"
    print(f"\n[MONTHLY REVIEW] due (last: {ago}) — validating blended-score parameters...")
    mr = run(["scripts/monthly_review.py", "--days", "60"])
    if mr.returncode != 0:
        print(f"  review FAILED rc={mr.returncode}: {mr.stderr.strip()[-200:]}")
        return False

    # Report from the written JSON, not by grepping stdout (the old filter matched only the bare
    # section HEADERS, so the loop printed "Performance:"/"Neutrality:" with no numbers at all).
    ts = _last_review_ts(review_dir)
    latest = None
    try:
        files = [f for f in os.listdir(review_dir) if f.endswith(".json")]
        latest = max(files) if files else None
    except OSError:
        pass
    if latest:
        try:
            print("  " + _review_summary_line(json.load(open(os.path.join(review_dir, latest)))))
            print(f"  report: state/monthly_review/{latest}")
        except Exception as e:  # noqa: BLE001 — never let reporting break the driver
            print(f"  review ran but its report is unreadable: {e}")
    if ts is None:
        # No run_ts and no readable mtime => the gate can't advance and would re-fire every tick.
        print("  WARNING: no review timestamp recorded — gate may re-fire next tick.")
    return True


def main() -> int:
    rl = run(["scripts/run_loops.py"])
    try:
        st = json.loads(rl.stdout)["strategic"]
    except Exception:  # noqa: BLE001 — data outage / lock contention -> hold the book, retry next
        longs, shorts = _book()
        print(f"HOLD-ON-DATA-OUTAGE: run_loops gave no verdict (rate-limit/network/lock) — book "
              f"held, retry next tick. LONG {'/'.join(longs)} vs SHORT {'/'.join(shorts)} | "
              f"err: {rl.stderr.strip()[-200:]}")
        return 0
    cycle = st.get("cycle")
    if not st.get("due"):
        longs, shorts = _book()
        flat = not longs or not shorts
        print(f"SKIP cycle {cycle} | {'FLAT!' if flat else 'deployed'} | "
              f"LONG {'/'.join(longs)} vs SHORT {'/'.join(shorts)}")

        # Monthly review check (optional, runs every ~30 days)
        _check_monthly_review()

        return 0

    cdir = os.path.join(ROOT, "state", "cycle", str(cycle))
    print(f"DUE cycle {cycle}: running deterministic blended tick")

    # BAN GUARD (self-heal): if a prior fire recorded an ACTIVE -1003 IP ban, HOLD before touching
    # the exchange — any fetch now would only re-extend the ban ~22min and it would never lapse
    # (observed cy190: the ban ratcheted ahead of real time across rapid fires). Wait it out; the
    # next fire landing after the deadline fetches cleanly. The 4h candle is still there to execute.
    _state = os.path.join(ROOT, "state")
    rem = _ban_remaining_ms(_state)
    if rem > 0:
        longs, shorts = _book()
        book = f"LONG {'/'.join(longs)} vs SHORT {'/'.join(shorts)}"
        print(f"HOLD-ON-DATA-OUTAGE cycle {cycle}: Binance -1003 ban still active for "
              f"~{rem // 60000}m — skipping scout to let it lapse (no fetch = no re-extend), book "
              f"held. {book}")
        return 0

    # A DATA OUTAGE (Binance rate-limit 418/-1003, network) makes scout/preflight produce no file.
    # That is transient, not a code bug: HOLD the book and retry next tick (exit 0 so the cron does
    # NOT flag it for investigation), never crash. The book is untouched — the gate has not run.
    sc = run(["scripts/scout_cli.py", "--cycle", str(cycle), "--top", "12"])
    upath = os.path.join(cdir, "universe.json")
    if not os.path.exists(upath):
        # record any -1003 ban deadline so the NEXT fire holds before fetching (self-heal)
        _bu = _parse_ban_until_ms(sc.stderr) or _parse_ban_until_ms(sc.stdout)
        if _bu:
            _record_ban(_state, _bu)
        longs, shorts = _book()
        book = f"LONG {'/'.join(longs)} vs SHORT {'/'.join(shorts)}"
        _bnote = f" (ban until {_bu}, holding next fires until it lapses)" if _bu else ""
        print(f"HOLD-ON-DATA-OUTAGE cycle {cycle}: scout produced no universe (Binance "
              f"rate-limit/network){_bnote} — book held, retry next tick. {book} | "
              f"err: {sc.stderr.strip()[-160:]}")
        return 0
    uni = json.load(open(upath))
    uni_syms = [s["symbol"] for s in uni.get("universe", uni.get("candidates", []))]
    symbols = list(dict.fromkeys(uni_syms + _held_symbols()))  # union, order-preserving
    pf = run(["scripts/preflight.py", "--cycle", str(cycle), "--symbols", ",".join(symbols)])
    if not os.path.exists(os.path.join(cdir, "context.json")):
        print(f"HOLD-ON-DATA-OUTAGE cycle {cycle}: preflight produced no context (rate-limit/"
              f"network) — book held, retry next tick. err: {pf.stderr.strip()[-200:]}")
        return 0

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
        print(f"HOLD-ON-DATA-OUTAGE cycle {cycle}: blended_book_cli produced no proposals — book "
              f"held, retry next tick. err: {bb.stderr.strip()[-300:]}")
        return 0
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
        print(f"HOLD-ON-DATA-OUTAGE cycle {cycle}: gate produced no report (rate-limit/network "
              f"mid-execute, or a parse issue) — book held, retry next tick.")
        return 0
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

    # Monthly review check (optional, runs every ~30 days)
    _check_monthly_review()

    return 0


if __name__ == "__main__":
    sys.exit(main())
