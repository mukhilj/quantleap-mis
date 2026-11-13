"""Import idempotency, identity and update/conflict rules, using temporary projects."""
import csv
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from qlmis import schema as S
from qlmis.config import load_settings
from qlmis.ingest import run_import, resolve_review, classify

ROOT = Path(__file__).resolve().parent.parent


def row(pid, client="Z1", strat="s1", sym="NIFTY26AUG24300CE", action="SELL", qty="65",
        ts="2026-08-17T10:00:00+05:30", entry="100", exit_="80", pnl=None, status="CLOSED",
        entry_status="COMPLETE", closed="2026-08-17", exec_="2026-08-17", exit_status="COMPLETE"):
    if pnl is None and entry_status == "COMPLETE":
        sign = 1 if action == "BUY" else -1
        pnl = str((float(exit_) - float(entry)) * float(qty) * sign)
    r = dict.fromkeys(S.QL_POSITIONS_COLUMNS, "")
    r.update(position_id=pid, execution_date=exec_, closed_date=closed, broker="zebu", client_id=client,
             basket_id="b", basket_name="B", strategy_id=strat, strategy_name="S", symbol=sym, exchange="NFO",
             product="NRML", action=action, lots="0", quantity=qty, entry_filled_at=ts,
             expected_entry_price=entry, average_entry_price=entry, exit_average_price=exit_, pnl=pnl or "0",
             exit_type="EXIT", position_status=status, entry_status=entry_status, exit_status=exit_status,
             expiry_date="2026-08-25")
    return r


def write(path: Path, rows, reverse=False):
    rows = list(reversed(rows)) if reverse else rows
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=S.QL_POSITIONS_COLUMNS)
        w.writeheader()
        w.writerows(rows)


@pytest.fixture
def proj(tmp_path):
    (tmp_path / "config").mkdir()
    shutil.copy(ROOT / "config" / "settings.toml", tmp_path / "config" / "settings.toml")
    for d in ("inbox", "archive", "data", "exports"):
        (tmp_path / d).mkdir()
    return tmp_path


def totals(s):
    con = sqlite3.connect(s.database)
    n, p = con.execute("SELECT COUNT(*), COALESCE(SUM(pnl),0) FROM legs WHERE entry_status='COMPLETE'").fetchone()
    con.close()
    return n, round(p, 2)


def test_same_file_renamed_reordered_and_overlap(proj):
    s = load_settings(proj)
    rows = [row("a"), row("b", ts="2026-08-17T11:00:00+05:30"), row("c", entry_status="CANCELLED", entry="0", exit_="0")]
    write(proj / "inbox" / "x.csv", rows)
    r1 = run_import(s)
    assert r1.status == "committed" and r1.counts["new"] == 3
    base = totals(s)
    assert base == (2, 2600.0)
    # same file again
    assert run_import(s).counts["new"] == 0 and totals(s) == base
    # renamed identical copy -> recognised by hash
    shutil.copy(proj / "inbox" / "x.csv", proj / "inbox" / "x_renamed.csv")
    r = run_import(s)
    assert [f.status for f in r.files].count("known") == 2 and totals(s) == base
    # reordered rows (different bytes) -> record-level duplicates
    write(proj / "inbox" / "x_reordered.csv", rows, reverse=True)
    r = run_import(s)
    assert r.counts["duplicate"] == 3 and r.counts["new"] == 0 and totals(s) == base
    # overlapping export with one genuinely new record
    write(proj / "inbox" / "y.csv", rows[:2] + [row("d", ts="2026-08-18T10:00:00+05:30", closed="2026-08-18")])
    r = run_import(s)
    assert r.counts == dict(new=1, duplicate=2, update=0, superseded=0, conflict=0, invalid=0)
    assert totals(s) == (3, 3900.0)


def test_numeric_formatting_is_not_a_change(proj):
    s = load_settings(proj)
    write(proj / "inbox" / "a.csv", [row("a", entry="100.50", exit_="80.0")])
    run_import(s)
    write(proj / "inbox" / "b.csv", [row("a", entry="100.5", exit_="80")])
    r = run_import(s)
    assert r.counts["duplicate"] == 1 and r.counts["conflict"] == 0


def test_identical_trades_in_two_accounts_are_separate(proj):
    s = load_settings(proj)
    write(proj / "inbox" / "a.csv", [row("same", client="Z1"), row("same", client="Z2")])
    r = run_import(s)
    assert r.counts["new"] == 2 and totals(s)[0] == 2


def test_repeated_fills_without_id_are_flagged_not_merged(proj):
    s = load_settings(proj)
    write(proj / "inbox" / "a.csv", [row(""), row("")])  # same fingerprint, no ID
    r = run_import(s)
    assert r.counts["new"] == 1 and r.counts["conflict"] == 1
    con = sqlite3.connect(s.database)
    q = con.execute("SELECT review_id, classification FROM review_queue").fetchall()
    con.close()
    assert q[0][1] == "ambiguous"
    # user confirms it is a genuine second fill -> becomes a distinct leg
    assert resolve_review(s, q[0][0], accept=True) == "ok"
    assert totals(s)[0] == 2


def test_open_to_closed_update_and_stale_snapshot(proj):
    s = load_settings(proj)
    write(proj / "inbox" / "a.csv", [row("a", status="OPEN", exit_="", pnl="", closed="", exit_status="")])
    run_import(s)
    write(proj / "inbox" / "b.csv", [row("a")])
    r = run_import(s)
    assert r.counts["update"] == 1 and totals(s) == (1, 1300.0)
    con = sqlite3.connect(s.database)
    assert con.execute("SELECT MAX(revision) FROM leg_revisions WHERE leg_key LIKE '%|a'").fetchone()[0] == 2
    con.close()
    # an old OPEN snapshot arriving later must not overwrite the closed record
    # (extra row 'z' makes the file bytes differ from a.csv, which is otherwise skipped by hash)
    write(proj / "inbox" / "c.csv", [row("a", status="OPEN", exit_="", pnl="", closed="", exit_status=""),
                                     row("z", ts="2026-08-17T12:00:00+05:30")])
    r = run_import(s)
    assert r.counts["superseded"] == 1 and r.counts["new"] == 1 and totals(s) == (2, 2600.0)


def test_correction_without_authority_is_conflict(proj):
    s = load_settings(proj)
    write(proj / "inbox" / "a.csv", [row("a", exit_="80")])
    run_import(s)
    write(proj / "inbox" / "b.csv", [row("a", exit_="70")])
    r = run_import(s)
    assert r.counts["conflict"] == 1 and totals(s) == (1, 1300.0)  # confirmed P&L unchanged
    assert r.conflict_pnl_impact == pytest.approx(650.0)


def test_invalid_rows_quarantined(proj):
    s = load_settings(proj)
    bad = row("z")
    bad["quantity"] = "abc"
    write(proj / "inbox" / "a.csv", [row("a"), bad])
    r = run_import(s)
    assert r.counts["invalid"] == 1 and totals(s)[0] == 1


def test_failed_import_rolls_back(proj, monkeypatch):
    s = load_settings(proj)
    write(proj / "inbox" / "a.csv", [row("a")])
    run_import(s)
    write(proj / "inbox" / "b.csv", [row("b"), row("c")])
    import qlmis.ingest as I
    orig = I._upsert_leg
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("disk full")
        return orig(*a, **k)
    monkeypatch.setattr(I, "_upsert_leg", boom)
    r = run_import(s)
    assert r.status == "failed" and totals(s) == (1, 1300.0)   # nothing partially published
    monkeypatch.setattr(I, "_upsert_leg", orig)
    r = run_import(s)                                          # restart: retry succeeds cleanly
    assert r.counts["new"] == 2 and totals(s)[0] == 3


def test_source_purge_does_not_delete_history(proj):
    s = load_settings(proj)
    write(proj / "inbox" / "old.csv", [row("a"), row("b", ts="2026-08-17T11:00:00+05:30")])
    run_import(s)
    (proj / "inbox" / "old.csv").unlink()
    write(proj / "inbox" / "new.csv", [row("b", ts="2026-08-17T11:00:00+05:30")])  # 'a' purged at source
    run_import(s)
    assert totals(s)[0] == 2


def test_backfill_older_export(proj):
    s = load_settings(proj)
    write(proj / "inbox" / "recent.csv", [row("b", closed="2026-09-01", exec_="2026-09-01",
                                              ts="2026-09-01T10:00:00+05:30")])
    run_import(s)
    write(proj / "inbox" / "older.csv", [row("a", closed="2026-06-01", exec_="2026-06-01",
                                             ts="2026-06-01T10:00:00+05:30")])
    r = run_import(s)
    assert r.counts["new"] == 1 and totals(s)[0] == 2


def test_rollup_snapshots_deduplicate(proj):
    s = load_settings(proj)
    hdr = S.ROLLUP_COLUMNS
    line = ["2026-11-13", "Z1", "account", "2026-04-01", "2026-11-13", "total_pnl", "12345", "net", "true"]
    for name in ("r1.csv", "r2.csv"):
        with open(proj / "inbox" / name, "w", newline="") as f:
            w = csv.writer(f); w.writerow(hdr); w.writerow(line)
        os.utime(proj / "inbox" / name)
    # different bytes (LF vs CRLF), same logical row -> must still dedupe at record level
    (proj / "inbox" / "r2.csv").write_bytes((",".join(hdr) + "\n" + ",".join(line) + "\n").encode())
    run_import(s)
    con = sqlite3.connect(s.database)
    assert con.execute("SELECT COUNT(*) FROM rollups").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM legs").fetchone()[0] == 0    # never become trades
    con.close()


def test_unrecognised_schema_flagged(proj):
    s = load_settings(proj)
    (proj / "inbox" / "weird.csv").write_text("foo,bar\n1,2\n")
    r = run_import(s)
    assert r.files[0].status == "unrecognised" and r.status == "committed"


def test_classify_rules():
    a = {c: "" for c in S.QL_POSITIONS_COLUMNS}
    a.update(client_id="Z", strategy_id="s", symbol="X", action="SELL", position_status="CLOSED", pnl="10",
             exit_average_price="5")
    b = dict(a, symbol="Y")
    assert classify(a, b)[0] == "conflict"
    c = dict(a, sl_status="CANCELLED")
    assert classify(a, c)[0] == "update"
    assert classify(c, a)[0] == "superseded"
