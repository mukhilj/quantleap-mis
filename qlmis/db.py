"""SQLite persistence: the permanent local ledger."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

-- One row per distinct source file content (identity = sha256, never the filename).
CREATE TABLE IF NOT EXISTS source_files (
    file_id        INTEGER PRIMARY KEY,
    sha256         TEXT NOT NULL UNIQUE,
    original_name  TEXT NOT NULL,
    original_path  TEXT NOT NULL,
    archived_path  TEXT,
    size_bytes     INTEGER,
    file_mtime_ist TEXT,           -- export as-of proxy
    schema_name    TEXT,
    row_count      INTEGER,
    status         TEXT NOT NULL,  -- imported | unrecognised | failed
    first_seen_at  TEXT NOT NULL,
    first_run_id   INTEGER
);

-- Every name/path a given file content has been seen under (renamed copies).
CREATE TABLE IF NOT EXISTS file_aliases (
    file_id INTEGER NOT NULL REFERENCES source_files(file_id),
    path    TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    PRIMARY KEY (file_id, path)
);

CREATE TABLE IF NOT EXISTS import_runs (
    run_id        INTEGER PRIMARY KEY,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL,   -- running | committed | failed | preview
    files_seen    INTEGER DEFAULT 0,
    files_new     INTEGER DEFAULT 0,
    files_known   INTEGER DEFAULT 0,
    rows_processed INTEGER DEFAULT 0,
    n_new INTEGER DEFAULT 0, n_duplicate INTEGER DEFAULT 0, n_update INTEGER DEFAULT 0,
    n_superseded INTEGER DEFAULT 0, n_conflict INTEGER DEFAULT 0, n_invalid INTEGER DEFAULT 0,
    confirmed_legs_before INTEGER, confirmed_legs_after INTEGER,
    confirmed_pnl_before REAL, confirmed_pnl_after REAL,
    report_json   TEXT,
    message       TEXT
);

-- Confirmed ledger of option-leg positions. One row per source identity.
CREATE TABLE IF NOT EXISTS legs (
    leg_key        TEXT PRIMARY KEY,   -- platform|client_id|position_id  (or fp:... fallback)
    platform       TEXT NOT NULL,
    client_id      TEXT NOT NULL,
    position_id    TEXT NOT NULL,
    identity_basis TEXT NOT NULL,      -- source_id | fingerprint
    execution_date TEXT, closed_date TEXT, broker TEXT,
    basket_id TEXT, basket_name TEXT, strategy_id TEXT, strategy_name TEXT,
    underlying TEXT, symbol TEXT, exchange TEXT, product TEXT, trading_type TEXT,
    intraday_exit_mode TEXT, action TEXT, lots REAL, quantity REAL,
    entry_filled_at TEXT, expected_entry_price REAL, average_entry_price REAL,
    exit_average_price REAL, pnl REAL, exit_type TEXT, position_status TEXT,
    entry_status TEXT, exit_status TEXT, sl_status TEXT, expiry_date TEXT,
    content_hash   TEXT NOT NULL,
    revision       INTEGER NOT NULL DEFAULT 1,
    first_file_id  INTEGER, first_row INTEGER, first_run_id INTEGER,
    last_file_id   INTEGER, last_row INTEGER, updated_run_id INTEGER,
    UNIQUE (platform, client_id, position_id)
);
CREATE INDEX IF NOT EXISTS ix_legs_strategy ON legs(client_id, strategy_id);
CREATE INDEX IF NOT EXISTS ix_legs_closed ON legs(closed_date);

-- Audit trail: every accepted version of every leg.
CREATE TABLE IF NOT EXISTS leg_revisions (
    leg_key TEXT NOT NULL, revision INTEGER NOT NULL, change_type TEXT NOT NULL,
    changed_fields TEXT, content_hash TEXT NOT NULL, payload_json TEXT NOT NULL,
    file_id INTEGER, row_num INTEGER, run_id INTEGER, recorded_at TEXT NOT NULL,
    PRIMARY KEY (leg_key, revision)
);

-- Provenance: every (file,row) where a leg identity was observed.
CREATE TABLE IF NOT EXISTS sightings (
    leg_key TEXT NOT NULL, file_id INTEGER NOT NULL, row_num INTEGER NOT NULL,
    content_hash TEXT NOT NULL, classification TEXT NOT NULL,
    PRIMARY KEY (file_id, row_num)
);
CREATE INDEX IF NOT EXISTS ix_sightings_leg ON sightings(leg_key);

-- Conflicts / invalid / ambiguous rows kept for review; never in confirmed results.
CREATE TABLE IF NOT EXISTS review_queue (
    review_id INTEGER PRIMARY KEY,
    run_id INTEGER, file_id INTEGER, row_num INTEGER,
    leg_key TEXT, classification TEXT NOT NULL, reason TEXT,
    payload_json TEXT NOT NULL, pnl_impact REAL,
    status TEXT NOT NULL DEFAULT 'open',  -- open | accepted | rejected
    resolved_at TEXT, resolution_note TEXT
);

-- Account coverage evidence: each file proves complete detail for [cover_start, cover_end].
CREATE TABLE IF NOT EXISTS coverage (
    file_id INTEGER NOT NULL, client_id TEXT NOT NULL,
    cover_start TEXT NOT NULL, cover_end TEXT NOT NULL,
    last_day_partial INTEGER NOT NULL DEFAULT 0,
    basis TEXT,
    PRIMARY KEY (file_id, client_id)
);

-- User-defined named baskets (independent of QuantLead's own basket_name).
CREATE TABLE IF NOT EXISTS baskets (
    basket_id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
    description TEXT, origin TEXT DEFAULT 'user', created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS basket_members (
    basket_id INTEGER NOT NULL REFERENCES baskets(basket_id) ON DELETE CASCADE,
    client_id TEXT NOT NULL, strategy_id TEXT NOT NULL,
    PRIMARY KEY (basket_id, client_id, strategy_id)
);

-- Source-reported aggregates. Kept apart from detailed legs; never summed into them.
CREATE TABLE IF NOT EXISTS rollups (
    rollup_id INTEGER PRIMARY KEY, file_id INTEGER, source TEXT, snapshot_date TEXT,
    client_id TEXT, scope TEXT, period_start TEXT, period_end TEXT,
    metric TEXT NOT NULL, value REAL, basis TEXT, is_cumulative INTEGER,
    content_hash TEXT NOT NULL UNIQUE
);
"""


def connect(path: Path) -> sqlite3.Connection:
    """Open (and create/upgrade) the ledger database."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, isolation_level=None, timeout=30)  # autocommit; explicit BEGIN for txns
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    # DELETE journal (not WAL): a single file is safer inside a OneDrive-synced folder.
    con.execute("PRAGMA journal_mode=DELETE")
    con.executescript(DDL)
    con.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
    return con
