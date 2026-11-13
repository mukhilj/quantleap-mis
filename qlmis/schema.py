"""Recognised source schemas, normalisation and row validation."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# QuantLead "positions" export (one row per option-leg position).
QL_POSITIONS_COLUMNS = [
    "position_id", "execution_date", "closed_date", "broker", "client_id", "basket_id",
    "basket_name", "strategy_id", "strategy_name", "underlying", "symbol", "exchange",
    "product", "trading_type", "intraday_exit_mode", "action", "lots", "quantity",
    "entry_filled_at", "expected_entry_price", "average_entry_price", "exit_average_price",
    "pnl", "exit_type", "position_status", "entry_status", "exit_status", "sl_status",
    "expiry_date",
]
NUMERIC = ["lots", "quantity", "expected_entry_price", "average_entry_price", "exit_average_price", "pnl"]
DATES = ["execution_date", "closed_date", "expiry_date"]
TIMESTAMPS = ["entry_filled_at"]
# Fields that, once set, define what the position *is*. A change means a conflict, not an update.
IMMUTABLE = ["client_id", "strategy_id", "symbol", "action", "exchange", "execution_date", "expiry_date"]
# Entry-side immutable fields used for the fallback fingerprint when position_id is missing.
FINGERPRINT_FIELDS = ["client_id", "strategy_id", "symbol", "action", "quantity",
                      "execution_date", "entry_filled_at", "average_entry_price"]

# Generic aggregate/rollup CSV (for source-reported KPIs such as QuantLead Top Trades/Distribution).
ROLLUP_COLUMNS = ["snapshot_date", "client_id", "scope", "period_start", "period_end",
                  "metric", "value", "basis", "is_cumulative"]

SCHEMAS = {
    "quantlead_positions_v1": set(QL_POSITIONS_COLUMNS),
    "rollup_v1": set(ROLLUP_COLUMNS),
}


def detect_schema(columns: list[str]) -> str | None:
    """Return schema name if the header contains all required columns."""
    cols = {c.strip().lstrip("﻿") for c in columns}
    for name, req in SCHEMAS.items():
        if req <= cols:
            return name
    return None


def canon_number(v: str) -> str:
    """Canonical decimal text: '46.20' and '46.2' -> '46.2'. Raises on garbage. No rounding."""
    v = v.strip().replace(",", "")
    if v == "":
        return ""
    d = Decimal(v)
    if not d.is_finite():
        raise InvalidOperation(v)
    t = format(d.normalize(), "f")
    return "0" if t in ("-0", "") else t


def canon_date(v: str) -> str:
    v = v.strip()
    return "" if v == "" else datetime.strptime(v[:10], "%Y-%m-%d").date().isoformat()


def canon_ts(v: str) -> str:
    """Timestamp -> ISO in IST. Naive timestamps are assumed IST (source convention)."""
    v = v.strip()
    if v == "":
        return ""
    dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST).isoformat()


@dataclass
class RowResult:
    ok: bool
    record: dict          # normalised strings
    errors: list[str]
    warnings: list[str]


def normalise_position(raw: dict) -> RowResult:
    """Normalise one QuantLead positions row and validate it."""
    rec, errs, warns = {}, [], []
    for c in QL_POSITIONS_COLUMNS:
        v = (raw.get(c) or "").strip()
        v = re.sub(r"\s+", " ", v)  # collapse internal whitespace (e.g. 'STBT  Hedges')
        try:
            if c in NUMERIC:
                v = canon_number(v)
            elif c in DATES:
                v = canon_date(v)
            elif c in TIMESTAMPS:
                v = canon_ts(v)
        except (InvalidOperation, ValueError):
            errs.append(f"{c}: cannot parse {raw.get(c)!r}")
        rec[c] = v
    rec["client_id"] = rec["client_id"].upper()
    rec["action"] = rec["action"].upper()
    if not rec["client_id"]:
        errs.append("client_id missing")
    if rec["action"] not in ("BUY", "SELL"):
        errs.append(f"action invalid: {rec['action']!r}")
    if not rec["closed_date"] and rec["position_status"] == "CLOSED":
        errs.append("closed position without closed_date")
    if not errs and rec["entry_status"] == "COMPLETE":
        if not rec["entry_filled_at"]:
            errs.append("filled entry without entry_filled_at")
        if not rec["quantity"] or Decimal(rec["quantity"]) <= 0:
            errs.append("filled entry with non-positive quantity")
        if rec["position_status"] == "CLOSED" and rec["pnl"] != "":
            # Data-quality check: stated P&L vs price arithmetic.
            sign = 1 if rec["action"] == "BUY" else -1
            calc = (Decimal(rec["exit_average_price"] or 0) - Decimal(rec["average_entry_price"] or 0)) \
                * Decimal(rec["quantity"]) * sign
            if abs(calc - Decimal(rec["pnl"])) > 1:
                warns.append(f"pnl {rec['pnl']} differs from price arithmetic {calc}")
    return RowResult(not errs, rec, errs, warns)


def content_hash(rec: dict) -> str:
    """Hash of the full normalised record (all source fields, sorted)."""
    return hashlib.sha256(json.dumps(rec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def fingerprint(rec: dict) -> str:
    return hashlib.sha256("|".join(rec.get(f, "") for f in FINGERPRINT_FIELDS).encode()).hexdigest()[:24]


def leg_key(platform: str, rec: dict) -> tuple[str, str]:
    """(leg_key, identity_basis). Prefer source position_id; fall back to a fingerprint."""
    if rec["position_id"]:
        return f"{platform}|{rec['client_id']}|{rec['position_id']}", "source_id"
    return f"{platform}|{rec['client_id']}|fp:{fingerprint(rec)}", "fingerprint"


FILENAME_RANGE = re.compile(r"(\d{4}-\d{2}-\d{2})_to_(\d{4}-\d{2}-\d{2})")


def filename_range(name: str) -> tuple[str | None, str | None]:
    """Requested export range embedded in QuantLead filenames, if present."""
    m = FILENAME_RANGE.search(name)
    return (m.group(1), m.group(2)) if m else (None, None)
