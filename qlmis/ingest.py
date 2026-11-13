"""Duplicate-safe, transactional import of source exports into the ledger.

Pipeline per run:
  discover -> hash each file -> skip contents already imported -> parse/validate rows ->
  classify every row against the ledger (new / duplicate / update / superseded / conflict / invalid)
  -> apply inside ONE transaction -> archive source copy -> commit (or rollback on any error).
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from . import schema as S
from .config import Settings, sync_aliases
from .db import connect

LEG_COLS = S.QL_POSITIONS_COLUMNS
NUM_SET = set(S.NUMERIC)


def now_ist() -> str:
    return datetime.now(S.IST).isoformat(timespec="seconds")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def discover(s: Settings) -> list[Path]:
    """CSV files directly inside configured input dirs, excluding generated/archived folders."""
    excl = {(s.root / d).resolve() for d in s.exclude_dirs} | {s.archive_dir.resolve(), s.export_dir.resolve()}
    out: list[Path] = []
    for d in s.input_dirs:
        d = d.resolve()
        if not d.exists() or d in excl:
            continue
        for p in sorted(d.glob("*.csv")):
            if p.is_file() and not any(parent in excl for parent in p.resolve().parents):
                out.append(p.resolve())
    # De-duplicate the same path reached via two input dirs.
    return sorted(set(out), key=lambda p: (p.stat().st_mtime, p.name))


@dataclass
class FileReport:
    path: str
    sha256: str
    status: str                  # new | known | unrecognised | failed
    schema: str | None = None
    rows: int = 0
    counts: dict = field(default_factory=dict)
    date_min: str | None = None
    date_max: str | None = None
    accounts: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


@dataclass
class ImportReport:
    run_id: int | None
    dry_run: bool
    status: str
    files: list[FileReport] = field(default_factory=list)
    counts: dict = field(default_factory=lambda: dict(new=0, duplicate=0, update=0, superseded=0,
                                                      conflict=0, invalid=0))
    legs_before: int = 0
    legs_after: int = 0
    pnl_before: float = 0.0
    pnl_after: float = 0.0
    conflict_pnl_impact: float = 0.0
    message: str = ""

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["files"] = [f.__dict__ for f in self.files]
        return d


def _read_csv(p: Path) -> tuple[list[str], list[dict]]:
    text = p.read_bytes().decode("utf-8-sig")
    rdr = csv.DictReader(io.StringIO(text))
    header = [h.strip().lstrip("﻿") for h in (rdr.fieldnames or [])]
    rdr.fieldnames = header
    return header, list(rdr)


def _confirmed_totals(con) -> tuple[int, float]:
    r = con.execute("SELECT COUNT(*), COALESCE(SUM(pnl),0) FROM legs WHERE entry_status='COMPLETE'").fetchone()
    return int(r[0]), float(r[1])


def _rank(rec: dict) -> tuple[int, int]:
    """Snapshot finality: CLOSED beats non-closed; then more populated exit fields."""
    closed = 1 if rec.get("position_status") == "CLOSED" else 0
    filled = sum(1 for c in ("exit_status", "exit_average_price", "closed_date", "sl_status", "exit_type")
                 if rec.get(c))
    return closed, filled


def classify(old: dict, new: dict) -> tuple[str, list[str], str]:
    """Compare a stored record with an incoming one for the same identity.

    Returns (classification, changed_fields, reason). Authority rules are explicit;
    file processing order never decides a winner.
    """
    changed = [c for c in LEG_COLS if (old.get(c) or "") != (new.get(c) or "")]
    if not changed:
        return "duplicate", [], ""
    imm = [c for c in changed if c in S.IMMUTABLE and old.get(c)]
    if imm:
        return "conflict", changed, f"immutable field(s) changed: {', '.join(imm)}"
    ro, rn = _rank(old), _rank(new)
    if ro[0] == 0 and rn[0] == 1:
        return "update", changed, "open-to-closed transition"
    if ro[0] == 1 and rn[0] == 0:
        return "superseded", changed, "older open snapshot of a closed position"
    fills_blanks = all(not old.get(c) for c in changed)      # new only adds information
    loses_info = all(not new.get(c) for c in changed)        # new only lacks information
    if fills_blanks:
        return "update", changed, "fields populated in later snapshot"
    if loses_info:
        return "superseded", changed, "incoming snapshot lacks fields already known"
    # Same-status record with different filled values, e.g. corrected price or P&L.
    return "conflict", changed, "values differ with no authority rule (correction?)"


def _pnl(rec: dict) -> float:
    try:
        return float(rec.get("pnl") or 0) if rec.get("entry_status") == "COMPLETE" else 0.0
    except ValueError:
        return 0.0


def _upsert_leg(con, key, basis, platform, rec, chash, revision, file_id, row_num, run_id, change_type, changed):
    vals = {c: (float(rec[c]) if c in NUM_SET and rec[c] != "" else (None if c in NUM_SET else rec[c]))
            for c in LEG_COLS}
    if not vals["position_id"]:
        # Fingerprint identity: store the derived id so the UNIQUE constraint still holds.
        vals["position_id"] = key.split("|", 2)[2]
    if revision == 1:
        cols = ["leg_key", "platform", "identity_basis"] + LEG_COLS + [
            "content_hash", "revision", "first_file_id", "first_row", "first_run_id",
            "last_file_id", "last_row", "updated_run_id"]
        row = [key, platform, basis] + [vals[c] for c in LEG_COLS] + [
            chash, 1, file_id, row_num, run_id, file_id, row_num, run_id]
        con.execute(f"INSERT INTO legs({','.join(cols)}) VALUES({','.join('?' * len(cols))})", row)
    else:
        sets = ",".join(f"{c}=?" for c in LEG_COLS)
        con.execute(
            f"UPDATE legs SET {sets}, content_hash=?, revision=?, last_file_id=?, last_row=?, updated_run_id=? "
            f"WHERE leg_key=?",
            [vals[c] for c in LEG_COLS] + [chash, revision, file_id, row_num, run_id, key])
    con.execute(
        "INSERT INTO leg_revisions VALUES(?,?,?,?,?,?,?,?,?,?)",
        (key, revision, change_type, json.dumps(changed), chash, json.dumps(rec, sort_keys=True),
         file_id, row_num, run_id, now_ist()))


def _file_coverage(s: Settings, name: str, mtime_ist: datetime, recs: list[dict]) -> list[tuple]:
    """Coverage evidence per account in one positions export.

    Detail is taken as complete from max(requested start, earliest close date in the file)
    to the export as-of date. Retention purges by close date, so earlier closes are absent.
    """
    if not recs:
        return []
    req_start, req_end = S.filename_range(name)
    closes = [r["closed_date"] for r in recs if r["closed_date"]]
    if not closes:
        return []
    start = max(filter(None, [req_start, min(closes)]))
    asof_day = mtime_ist.date().isoformat()
    end = min(filter(None, [req_end, asof_day])) if req_end else asof_day
    end = max(end, max(closes))  # never claim less than what is present
    hh, mm = map(int, s.session_close_ist.split(":"))
    partial = int(end == asof_day and (mtime_ist.hour, mtime_ist.minute) < (hh, mm))
    basis = (f"requested {req_start or '?'}..{req_end or '?'}; earliest close in file {min(closes)}; "
             f"as-of {mtime_ist.isoformat(timespec='minutes')}")
    accts = sorted({r["client_id"] for r in recs})
    return [(a, start, end, partial, basis) for a in accts]


def run_import(s: Settings, dry_run: bool = False, paths: list[Path] | None = None) -> ImportReport:
    """Import all discoverable sources. dry_run=True produces the preview and rolls back."""
    con = connect(s.database)
    files = paths if paths is not None else discover(s)
    rep = ImportReport(run_id=None, dry_run=dry_run, status="running")
    rep.legs_before, rep.pnl_before = _confirmed_totals(con)
    archived_now: list[Path] = []
    try:
        con.execute("BEGIN IMMEDIATE")
        cur = con.execute("INSERT INTO import_runs(started_at,status) VALUES(?,?)",
                          (now_ist(), "preview" if dry_run else "running"))
        run_id = rep.run_id = cur.lastrowid
        new_strategies: dict[tuple, tuple] = {}

        for p in files:
            p = Path(p)
            digest = sha256_file(p)
            fr = FileReport(path=str(p), sha256=digest, status="new")
            known = con.execute("SELECT file_id,status FROM source_files WHERE sha256=?", (digest,)).fetchone()
            if known and known["status"] == "imported":
                # Same bytes already imported (possibly under another name) -> skip, remember the path.
                fr.status = "known"
                con.execute("INSERT OR IGNORE INTO file_aliases VALUES(?,?,?)", (known["file_id"], str(p), now_ist()))
                rep.files.append(fr)
                continue

            header, rows = _read_csv(p)
            fr.schema = S.detect_schema(header)
            fr.rows = len(rows)
            mtime = datetime.fromtimestamp(p.stat().st_mtime, S.IST)
            archived = s.archive_dir / f"{digest[:16]}__{p.name}"
            if known:
                file_id = known["file_id"]
                con.execute("UPDATE source_files SET schema_name=?, row_count=?, status=? WHERE file_id=?",
                            (fr.schema, fr.rows, "processing", file_id))
            else:
                file_id = con.execute(
                    "INSERT INTO source_files(sha256,original_name,original_path,archived_path,size_bytes,"
                    "file_mtime_ist,schema_name,row_count,status,first_seen_at,first_run_id) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (digest, p.name, str(p), str(archived.relative_to(s.root)) if archived.is_relative_to(s.root) else str(archived),
                     p.stat().st_size, mtime.isoformat(timespec="seconds"), fr.schema, fr.rows, "processing",
                     now_ist(), run_id)).lastrowid
            con.execute("INSERT OR IGNORE INTO file_aliases VALUES(?,?,?)", (file_id, str(p), now_ist()))

            if fr.schema is None:
                fr.status = "unrecognised"
                fr.warnings.append(f"Unrecognised columns: {header[:8]}...")
                con.execute("UPDATE source_files SET status='unrecognised' WHERE file_id=?", (file_id,))
                rep.files.append(fr)
                continue

            if fr.schema == "rollup_v1":
                _import_rollups(con, file_id, rows, fr)
            else:
                _import_positions(s, con, run_id, file_id, rows, fr, rep, mtime, p.name, new_strategies)

            con.execute("UPDATE source_files SET status='imported' WHERE file_id=?", (file_id,))
            if not dry_run and not archived.exists():
                s.archive_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, archived)
                archived_now.append(archived)
            rep.files.append(fr)

        rep.legs_after, rep.pnl_after = _confirmed_totals(con)
        rep.status = "preview" if dry_run else "committed"
        con.execute(
            "UPDATE import_runs SET finished_at=?, status=?, files_seen=?, files_new=?, files_known=?, "
            "rows_processed=?, n_new=?, n_duplicate=?, n_update=?, n_superseded=?, n_conflict=?, n_invalid=?, "
            "confirmed_legs_before=?, confirmed_legs_after=?, confirmed_pnl_before=?, confirmed_pnl_after=?, "
            "report_json=? WHERE run_id=?",
            (now_ist(), rep.status, len(rep.files), sum(f.status == "new" for f in rep.files),
             sum(f.status == "known" for f in rep.files), sum(f.rows for f in rep.files),
             *[rep.counts[k] for k in ("new", "duplicate", "update", "superseded", "conflict", "invalid")],
             rep.legs_before, rep.legs_after, rep.pnl_before, rep.pnl_after,
             json.dumps(rep.to_dict(), default=str), run_id))
        if dry_run:
            con.execute("ROLLBACK")
        else:
            con.execute("COMMIT")
            if new_strategies:
                import pandas as pd
                sync_aliases(s, pd.DataFrame(
                    [dict(client_id=k[0], strategy_id=k[1], strategy_name=v[0], basket_name=v[1])
                     for k, v in new_strategies.items()]))
                seed_source_baskets(con)
    except Exception as e:  # any failure -> nothing published
        if con.in_transaction:
            con.execute("ROLLBACK")
        rep.status = "failed"
        rep.message = f"{type(e).__name__}: {e}"
        con.execute("INSERT INTO import_runs(started_at,finished_at,status,message) VALUES(?,?,?,?)",
                    (now_ist(), now_ist(), "failed", rep.message))
        rep.legs_after, rep.pnl_after = _confirmed_totals(con)
    finally:
        con.close()
    return rep


def _import_positions(s, con, run_id, file_id, rows, fr, rep, mtime, name, new_strategies):
    counts = dict(new=0, duplicate=0, update=0, superseded=0, conflict=0, invalid=0)
    good: list[dict] = []
    seen_in_file: dict[str, int] = {}
    for i, raw in enumerate(rows, start=2):  # row_num = physical CSV line (header is line 1)
        res = S.normalise_position(raw)
        rec = res.record
        fr.warnings += [f"line {i}: {w}" for w in res.warnings]
        if not res.ok:
            counts["invalid"] += 1
            con.execute("INSERT INTO review_queue(run_id,file_id,row_num,leg_key,classification,reason,payload_json)"
                        " VALUES(?,?,?,?,?,?,?)",
                        (run_id, file_id, i, None, "invalid", "; ".join(res.errors), json.dumps(raw)))
            continue
        key, basis = S.leg_key(s.platform, rec)
        chash = S.content_hash(rec)
        # Fingerprint collisions inside one file may be genuine repeated fills -> cannot decide.
        if basis == "fingerprint" and key in seen_in_file:
            counts["conflict"] += 1
            con.execute("INSERT INTO review_queue(run_id,file_id,row_num,leg_key,classification,reason,payload_json,pnl_impact)"
                        " VALUES(?,?,?,?,?,?,?,?)",
                        (run_id, file_id, i, key, "ambiguous",
                         f"no position_id and fingerprint repeats line {seen_in_file[key]} in same file",
                         json.dumps(rec), _pnl(rec)))
            rep.conflict_pnl_impact += _pnl(rec)
            continue
        if basis == "source_id" and key in seen_in_file:
            # Same position_id twice in one file: compare like any other snapshot below.
            pass
        seen_in_file[key] = i
        good.append(rec)

        old = con.execute("SELECT * FROM legs WHERE leg_key=?", (key,)).fetchone()
        if old is None:
            cls, changed, reason = "new", [], ""
        else:
            oldrec = {c: _to_text(c, old[c]) for c in LEG_COLS}
            if basis == "fingerprint":
                oldrec["position_id"] = rec["position_id"]  # stored id is derived, not source content
            if old["content_hash"] == chash:
                cls, changed, reason = "duplicate", [], ""
            else:
                cls, changed, reason = classify(oldrec, rec)
        counts[cls] += 1
        con.execute("INSERT OR REPLACE INTO sightings VALUES(?,?,?,?,?)", (key, file_id, i, chash, cls))
        if cls == "new":
            _upsert_leg(con, key, basis, s.platform, rec, chash, 1, file_id, i, run_id, "insert", [])
            new_strategies.setdefault((rec["client_id"], rec["strategy_id"]), (rec["strategy_name"], rec["basket_name"]))
        elif cls == "update":
            _upsert_leg(con, key, basis, s.platform, rec, chash, old["revision"] + 1, file_id, i, run_id,
                        reason, changed)
        elif cls == "conflict":
            impact = _pnl(rec) - (old["pnl"] or 0 if old["entry_status"] == "COMPLETE" else 0)
            rep.conflict_pnl_impact += impact
            con.execute("INSERT INTO review_queue(run_id,file_id,row_num,leg_key,classification,reason,payload_json,pnl_impact)"
                        " VALUES(?,?,?,?,?,?,?,?)",
                        (run_id, file_id, i, key, "conflict", f"{reason}; fields: {', '.join(changed)}",
                         json.dumps(rec), impact))
    fr.counts = counts
    for k, v in counts.items():
        rep.counts[k] += v
    if good:
        closes = [r["closed_date"] for r in good if r["closed_date"]]
        fr.date_min, fr.date_max = (min(closes), max(closes)) if closes else (None, None)
        fr.accounts = sorted({r["client_id"] for r in good})
        for a, start, end, partial, basis in _file_coverage(s, name, mtime, good):
            con.execute("INSERT OR REPLACE INTO coverage VALUES(?,?,?,?,?,?)", (file_id, a, start, end, partial, basis))


def _to_text(c, v) -> str:
    """DB value -> normalised text, matching schema.normalise_position output."""
    if v is None:
        return ""
    if c in NUM_SET:
        return S.canon_number(repr(float(v)))
    return str(v)


def _import_rollups(con, file_id, rows, fr):
    """Source-reported aggregates. Stored as-is; repeated snapshots dedupe by content."""
    n_new = n_dup = 0
    for raw in rows:
        rec = {c: (raw.get(c) or "").strip() for c in S.ROLLUP_COLUMNS}
        h = S.content_hash(rec)
        try:
            val = float(rec["value"]) if rec["value"] else None
        except ValueError:
            val = None
        cur = con.execute(
            "INSERT OR IGNORE INTO rollups(file_id,source,snapshot_date,client_id,scope,period_start,period_end,"
            "metric,value,basis,is_cumulative,content_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (file_id, "quantlead", rec["snapshot_date"], rec["client_id"], rec["scope"], rec["period_start"],
             rec["period_end"], rec["metric"], val, rec["basis"],
             1 if rec["is_cumulative"].lower() in ("1", "true", "yes", "y") else 0, h))
        n_new += cur.rowcount
        n_dup += 1 - cur.rowcount
    fr.counts = {"rollup_new": n_new, "rollup_duplicate": n_dup}


def seed_source_baskets(con) -> None:
    """Create/extend one named basket per QuantLead basket ('QL · <name>'). User baskets untouched."""
    rows = con.execute("SELECT DISTINCT client_id, basket_name, strategy_id FROM legs").fetchall()
    for r in rows:
        name = f"QL · {r['basket_name']}"
        b = con.execute("SELECT basket_id, origin FROM baskets WHERE name=?", (name,)).fetchone()
        if b is None:
            bid = con.execute("INSERT INTO baskets(name,description,origin,created_at,updated_at) VALUES(?,?,?,?,?)",
                              (name, "Seeded from QuantLead basket_name", "source", now_ist(), now_ist())).lastrowid
        elif b["origin"] != "source":
            continue
        else:
            bid = b["basket_id"]
        con.execute("INSERT OR IGNORE INTO basket_members VALUES(?,?,?)", (bid, r["client_id"], r["strategy_id"]))


def resolve_review(s: Settings, review_id: int, accept: bool, note: str = "") -> str:
    """Accept (apply incoming record as a new revision) or reject a conflict."""
    con = connect(s.database)
    try:
        con.execute("BEGIN IMMEDIATE")
        r = con.execute("SELECT * FROM review_queue WHERE review_id=? AND status='open'", (review_id,)).fetchone()
        if r is None:
            con.execute("ROLLBACK")
            return "not found or already resolved"
        if accept and r["classification"] in ("conflict", "ambiguous"):
            rec = json.loads(r["payload_json"])
            chash = S.content_hash(rec)
            old = con.execute("SELECT revision FROM legs WHERE leg_key=?", (r["leg_key"],)).fetchone()
            if r["classification"] == "ambiguous" or old is None:
                _, basis = S.leg_key(s.platform, rec)
                # Ambiguous fingerprint accepted as a distinct leg: disambiguate the key by source row.
                key = f"{r['leg_key']}#f{r['file_id']}r{r['row_num']}"
                _upsert_leg(con, key, basis + "+manual", s.platform, rec, chash, 1, r["file_id"], r["row_num"],
                            r["run_id"], "manual_accept", [])
            else:
                _upsert_leg(con, r["leg_key"], None, s.platform, rec, chash, old["revision"] + 1, r["file_id"],
                            r["row_num"], r["run_id"], "manual_accept", [c for c in LEG_COLS])
        con.execute("UPDATE review_queue SET status=?, resolved_at=?, resolution_note=? WHERE review_id=?",
                    ("accepted" if accept else "rejected", now_ist(), note, review_id))
        con.execute("COMMIT")
        return "ok"
    except Exception:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()
