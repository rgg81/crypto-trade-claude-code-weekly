import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from reset_desk import do_reset  # noqa: E402


def test_reset_archives_old_state_and_writes_flat_book(tmp_path):
    s = tmp_path / "state"
    (s / "cycle" / "4").mkdir(parents=True)
    (s / "cycle" / "4" / "report.json").write_text("{}")
    (s / "fast" / "cycle" / "9").mkdir(parents=True)
    s.joinpath("account.json").write_text('{"balance": 9498.5, "peak_equity": 10180.3}')
    s.joinpath("positions.json").write_text('[{"symbol": "CLUSDT"}]')
    s.joinpath("pending_orders.json").write_text('[{"symbol": "XAGUSDT"}]')
    s.joinpath("equity-history.jsonl").write_text('{"e": 1}\n')
    s.joinpath(".run.lock").write_text("held")

    res = do_reset(s, start_balance=10_000.0, archive_ts="20260608T000000Z")

    # clean flat book written
    acct = json.loads((s / "account.json").read_text())
    assert acct["balance"] == 10_000.0 and acct["peak_equity"] == 10_000.0 and acct["halt"] is False
    assert json.loads((s / "positions.json").read_text()) == []
    assert json.loads((s / "pending_orders.json").read_text()) == []
    # old runtime state archived (recoverable), NOT deleted
    arc = s / "archive" / "reset_20260608T000000Z"
    assert (arc / "account.json").exists() and (arc / "cycle" / "4" / "report.json").exists()
    assert (arc / "positions.json").exists() and (arc / "pending_orders.json").exists()
    # the stale lock + histories are cleared from the live dir
    assert not (s / ".run.lock").exists() and not (s / "equity-history.jsonl").exists()
    assert not (s / "fast").exists() and not (s / "cycle").exists()
    assert "account.json" in res["archived"]
