"""Stdout/exit-code contract for scripts/due_check.py — the cron prompt branches on line 1."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "due_check.py"


def _current_candle_iso(tf_minutes: int = 240) -> str:
    now = datetime.now(UTC)
    msm = now.hour * 60 + now.minute
    floored = (msm // tf_minutes) * tf_minutes
    return now.replace(hour=floored // 60, minute=floored % 60, second=0, microsecond=0).isoformat()


def _run(state_dir: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(CLI), str(state_dir), *extra],
                          capture_output=True, text=True, cwd=ROOT)


def _report(state_dir: Path, n: int, candle: str, ran_at: str, *, loop: str | None = None) -> None:
    root = state_dir / loop / "cycle" if loop else state_dir / "cycle"
    d = root / str(n)
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.json").write_text(json.dumps({"cycle": n, "candle": candle, "ran_at": ran_at}))


def test_cli_cold_start_prints_due_fresh_exit0(tmp_path):
    r = _run(tmp_path)
    assert r.returncode == 0
    assert r.stdout.splitlines()[0].startswith("DUE FRESH ")


def test_cli_served_candle_prints_skip_exit0(tmp_path):
    # serving the CURRENT 4h candle guarantees SKIP regardless of when the test runs
    candle = _current_candle_iso()
    _report(tmp_path, 7, candle=candle, ran_at=candle)
    r = _run(tmp_path)
    assert r.returncode == 0
    assert r.stdout.splitlines()[0].startswith("SKIP:")


def test_cli_line1_is_machine_parseable(tmp_path):
    r = _run(tmp_path)
    first = r.stdout.splitlines()[0]
    # line 1 starts with exactly one of the three tokens the cron prompt keys on
    assert first.startswith(("DUE FRESH ", "DUE RETRY ", "SKIP:"))


def test_cli_loop_strategic_cold_start_due(tmp_path):
    # --loop reads the loop timeframe from config.yaml (strategic=4h); the strategic loop is the
    # main cycle on the LEGACY state/cycle root (where gate_execute_cli writes its served-candle)
    r = _run(tmp_path, "--loop", "strategic")
    assert r.returncode == 0
    assert r.stdout.splitlines()[0].startswith("DUE FRESH ")


def test_cli_loop_strategic_served_4h_candle_skips(tmp_path):
    # serving the current 4h candle at the LEGACY root -> SKIP (the gate-cli report lands here, and
    # the strategic due-gate must see it; this is the regression guard for the re-run-forever bug)
    candle = _current_candle_iso(240)
    _report(tmp_path, 5, candle=candle, ran_at=candle)  # loop=None -> state/cycle
    r = _run(tmp_path, "--loop", "strategic")
    assert r.returncode == 0
    assert r.stdout.splitlines()[0].startswith("SKIP:")


def test_cli_loop_fast_served_15m_candle_skips(tmp_path):
    candle = _current_candle_iso(15)  # current 15m candle for the fast loop
    _report(tmp_path, 3, candle=candle, ran_at=candle, loop="fast")
    r = _run(tmp_path, "--loop", "fast")
    assert r.returncode == 0
    assert r.stdout.splitlines()[0].startswith("SKIP:")


def test_cli_loops_are_independent(tmp_path):
    # serving the fast loop's candle must NOT mark the strategic loop served (separate cycle roots)
    c15 = _current_candle_iso(15)
    _report(tmp_path, 1, candle=c15, ran_at=c15, loop="fast")
    r = _run(tmp_path, "--loop", "strategic")
    assert r.stdout.splitlines()[0].startswith("DUE FRESH ")  # strategic still due
