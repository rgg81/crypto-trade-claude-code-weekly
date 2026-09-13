"""Single-flight run lock: exactly one writer at a time across the loops, with stale reclaim."""
from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from futures_fund.runlock import release, single_flight, try_acquire

_ROOT = Path(__file__).resolve().parents[1]
_CLI = _ROOT / "scripts" / "runlock_cli.py"


def _cli(*args):
    return subprocess.run([sys.executable, str(_CLI), *args], capture_output=True, text=True,
                          cwd=_ROOT)


def _reclaim_worker(state_dir, iso, q):
    # module-level so multiprocessing 'spawn' can pickle it; races to reclaim one stale lock
    from datetime import datetime as _dt

    from futures_fund.runlock import try_acquire as _ta
    ok, _ = _ta(state_dir, _dt.fromisoformat(iso), owner="racer")
    q.put(1 if ok else 0)


def test_acquire_is_exclusive(tmp_path):
    now = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
    ok1, prior1 = try_acquire(tmp_path, now)
    assert ok1 is True and prior1 is None
    # second acquire while held -> denied, sees the holder
    ok2, holder = try_acquire(tmp_path, now)
    assert ok2 is False and isinstance(holder, dict) and holder["owner"] == "runner"


def test_release_allows_reacquire(tmp_path):
    now = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
    assert try_acquire(tmp_path, now)[0] is True
    release(tmp_path)
    assert try_acquire(tmp_path, now)[0] is True  # free again after release


def test_release_is_idempotent(tmp_path):
    release(tmp_path)  # no lock present -> no error
    release(tmp_path)


def test_stale_lock_is_reclaimed(tmp_path):
    start = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
    assert try_acquire(tmp_path, start)[0] is True
    # a fresh fire 10 min later cannot reclaim (default stale 30 min)
    assert try_acquire(tmp_path, start + timedelta(minutes=10))[0] is False
    # 31 min later the lock is stale -> reclaimed, returns the prior (evicted) holder
    ok, prior = try_acquire(tmp_path, start + timedelta(minutes=31))
    assert ok is True and isinstance(prior, dict)


def test_future_skewed_start_ts_is_reclaimed(tmp_path):
    # a crashed holder whose start_ts is in the FUTURE (forward clock skew) must NOT be treated as a
    # live holder forever — a negative age is corrupt and reclaimable (mirrors the scheduling guard)
    now = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
    assert try_acquire(tmp_path, now + timedelta(minutes=40))[0] is True  # holder stamped 12:40
    ok, prior = try_acquire(tmp_path, now)  # reclaimer's clock is 12:00 -> holder age -40min
    assert ok is True and isinstance(prior, dict)


def test_reclaimed_lock_denies_the_old_holder(tmp_path):
    # after a stale reclaim, the lock holds a FRESH holder -> a second reclaim attempt is denied
    start = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
    assert try_acquire(tmp_path, start)[0] is True
    later = start + timedelta(minutes=31)
    assert try_acquire(tmp_path, later)[0] is True            # reclaim #1 wins
    assert try_acquire(tmp_path, later)[0] is False           # the new holder is fresh -> denied


def test_concurrent_stale_reclaim_has_exactly_one_winner(tmp_path):
    # the critical invariant: under N processes racing to reclaim ONE stale lock, EXACTLY ONE wins.
    import multiprocessing as mp
    import queue as _queue
    start = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
    try_acquire(tmp_path, start)  # plant a holder that will be stale at start+31min
    fire = (start + timedelta(minutes=31)).isoformat()
    n = 8
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    procs = [ctx.Process(target=_reclaim_worker, args=(str(tmp_path), fire, q)) for _ in range(n)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
    # drain with a TIMEOUT so a process that died before reporting (possible under heavy CPU load)
    # fails the test cleanly instead of HANGING the whole suite on a blocking q.get().
    results = []
    for _ in range(n):
        try:
            results.append(q.get(timeout=10))
        except _queue.Empty:
            break
    for p in procs:
        if p.is_alive():
            p.terminate()
    assert len(results) == n, f"only {len(results)}/{n} reclaimers reported (load hiccup)"
    assert sum(results) == 1, f"expected exactly one reclaimer to win, got {sum(results)}"


def test_single_flight_context_releases(tmp_path):
    now = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
    with single_flight(tmp_path, now) as ok:
        assert ok is True
        # nested fire is locked out while the block holds it
        assert try_acquire(tmp_path, now)[0] is False
    # released on exit -> acquirable again
    assert try_acquire(tmp_path, now)[0] is True


def test_single_flight_yields_false_when_held(tmp_path):
    now = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
    assert try_acquire(tmp_path, now)[0] is True  # pre-held by a "live run"
    with single_flight(tmp_path, now) as ok:
        assert ok is False  # stand down
    # the pre-held lock must SURVIVE (the context didn't own it, so it must not release it)
    assert try_acquire(tmp_path, now)[0] is False


# ---- lease refresh + owned release (auto_cycle holds the lock across a multi-minute tick) --------

def test_refresh_keeps_a_long_running_holder_from_being_reclaimed(tmp_path):
    """A DUE tick can legitimately outlast the 30-min stale window (60s warm timeouts x ~100
    symbols against a stalled proxy). Without a refresh, a second tick reclaims mid-execute."""
    from futures_fund.runlock import refresh
    start = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
    assert try_acquire(tmp_path, start)[0] is True
    assert refresh(tmp_path, start + timedelta(minutes=25)) is True
    assert try_acquire(tmp_path, start + timedelta(minutes=45))[0] is False, \
        "20 minutes after the refresh the holder is live, not stale"


def test_refresh_never_takes_over_another_processs_lock(tmp_path):
    from futures_fund.runlock import LOCK_NAME, refresh
    start = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
    lock = tmp_path / LOCK_NAME
    lock.write_text('{"pid": 999999999, "owner": "other", "start_ts": "2026-06-08T12:00:00+00:00"}')
    before = lock.read_text()
    assert refresh(tmp_path, start + timedelta(minutes=5)) is False
    assert lock.read_text() == before


def test_refresh_never_creates_a_lock(tmp_path):
    from futures_fund.runlock import LOCK_NAME, refresh
    assert refresh(tmp_path, datetime(2026, 6, 8, 12, 0, tzinfo=UTC)) is False
    assert not (tmp_path / LOCK_NAME).exists()


def test_owned_release_leaves_a_reclaimed_lock_alone(tmp_path):
    """A holder whose lease lapsed and was legitimately reclaimed must not delete the NEW holder's
    lock on its way out — that would let a third tick run beside the second."""
    from futures_fund.runlock import LOCK_NAME, release_if_owned
    lock = tmp_path / LOCK_NAME
    lock.write_text('{"pid": 999999999, "owner": "other", "start_ts": "2026-06-08T12:00:00+00:00"}')
    assert release_if_owned(tmp_path) is False
    assert lock.exists()


def test_owned_release_removes_our_own_lock(tmp_path):
    from futures_fund.runlock import LOCK_NAME, release_if_owned
    assert try_acquire(tmp_path, datetime(2026, 6, 8, 12, 0, tzinfo=UTC))[0] is True
    assert release_if_owned(tmp_path) is True
    assert not (tmp_path / LOCK_NAME).exists()


def test_cli_acquire_hold_release_cycle(tmp_path):
    # the strategic loop holds the lock ACROSS processes: acquire -> (cycle) -> release
    s = str(tmp_path)
    assert _cli("status", "--state", s).stdout.strip() == "FREE"
    assert _cli("acquire", "--owner", "strategic", "--state", s).stdout.strip() == "ACQUIRED"
    assert _cli("status", "--state", s).stdout.startswith("HELD:")
    # a concurrent fast fire (separate process) sees it held and stands down
    assert _cli("acquire", "--owner", "fast", "--state", s).stdout.startswith("LOCKED:")
    assert _cli("release", "--state", s).stdout.strip() == "RELEASED"
    assert _cli("status", "--state", s).stdout.strip() == "FREE"
