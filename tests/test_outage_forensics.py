"""A data outage must leave enough evidence to name the failing call.

Four ticks have now been lost to HOLD-ON-DATA-OUTAGE and the console keeps only a 200-300 char
TAIL of stderr — which is the bottom of a Python traceback, i.e. the socket frames every timeout
shares, never the ccxt/urllib frame that says WHICH endpoint stalled:

      File ".../socket.py", line 718, in readinto
        return self._sock.recv_into(b)
    TimeoutError: timed out

That tail is why the first diagnosis (funding burst) and the second (unretried load_markets) were
each only partly right: cy415 timed out in preflight AFTER the load_markets retry shipped. Guessing
a third fix from the same 200 characters would be guessing again.

The full stderr is already in hand — `run()` captures it — it was simply thrown away. Persisting it
next to the cycle's other artefacts costs nothing, cannot affect the book, and makes the NEXT
outage self-diagnosing.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from auto_cycle import save_outage  # noqa: E402


def test_the_full_stderr_is_persisted_not_a_tail(tmp_path):
    """The head names the endpoint; the tail is the same socket frames every time."""
    head = 'ccxt.base.errors.NetworkError: binanceusdm GET https://fapi.binance.com/fapi/v1/klines'
    middle = "\n".join(f"  frame {i}" for i in range(400))
    stderr = head + "\n" + middle + "\nTimeoutError: timed out"

    p = save_outage(str(tmp_path), 415, "preflight", stderr)

    body = Path(p).read_text()
    assert head in body, "the endpoint that actually failed must survive"
    assert "TimeoutError: timed out" in body, "and so must the exception"


def test_it_records_which_STAGE_failed(tmp_path):
    """preflight, xsection_book_cli and the gate fail differently and are fixed differently."""
    p = save_outage(str(tmp_path), 415, "preflight", "boom")
    assert "preflight" in Path(p).read_text()


def test_each_cycle_gets_its_own_file(tmp_path):
    a = save_outage(str(tmp_path), 415, "preflight", "first")
    b = save_outage(str(tmp_path), 416, "gate", "second")
    assert a != b
    assert Path(a).read_text().count("first") == 1
    assert "second" in Path(b).read_text()


def test_a_second_outage_in_the_SAME_cycle_appends_rather_than_overwrites(tmp_path):
    """cy413 failed in preflight and then again in the gate. Losing the first would hide half the
    story — and the retry cadence means one cycle can fail several times."""
    save_outage(str(tmp_path), 413, "preflight", "the preflight traceback")
    p = save_outage(str(tmp_path), 413, "gate", "the gate traceback")

    body = Path(p).read_text()
    assert "the preflight traceback" in body
    assert "the gate traceback" in body


def test_it_never_raises_and_never_breaks_the_hold_path(tmp_path):
    """Forensics are strictly a bonus. If writing fails, the book must still be HELD — an outage
    handler that itself raises would turn a safe hold into a crash.

    (An `assert x is None or True` here would be vacuous — it passes however the code behaves. The
    real assertion is that the call RETURNS rather than propagating, so these run bare and the test
    fails by raising.)
    """
    save_outage("/proc/definitely-not-writable", 415, "preflight", "x")
    save_outage(str(tmp_path), 415, "preflight", None)
    save_outage(str(tmp_path), 415, "preflight", "")

    # and a hostile stage name must not escape the cycle directory
    p = save_outage(str(tmp_path), 415, "../../etc/passwd", "x")
    if p is not None:
        assert tmp_path in Path(p).resolve().parents or Path(p).resolve().is_relative_to(tmp_path)


def test_all_THREE_outage_paths_persist_their_stderr():
    """A helper nothing calls is worse than none — it reads as covered. Assert each hold path in
    the shipped driver actually writes its forensics: preflight, the book CLI, and the gate.
    """
    src = (Path(__file__).resolve().parents[1] / "scripts" / "auto_cycle.py").read_text()
    for stage in ("preflight", "xsection_book_cli", "gate"):
        assert f'save_outage(_state, cycle, "{stage}"' in src or \
               f'save_outage(os.path.join(ROOT, "state"), cycle, "{stage}"' in src, \
               f"the {stage} outage path must persist its stderr"
