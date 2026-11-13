"""Coverage: which trading days have complete detailed history, per account.

Day statuses:
  covered  - account's detailed history is complete for the day (zero P&L = verified no-trade)
  partial  - export taken mid-session; more closes may follow in a later export
  missing  - no export proves completeness (unknown, NEVER zero)
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import numpy as np
import pandas as pd


def trading_days(start: date, end: date, holidays: set) -> list[date]:
    if start > end:
        return []
    return [d.date() for d in pd.bdate_range(start, end) if d.date() not in holidays]


def account_intervals(con: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT c.client_id, c.cover_start, c.cover_end, c.last_day_partial, c.basis, f.original_name "
        "FROM coverage c JOIN source_files f ON f.file_id=c.file_id ORDER BY c.client_id, c.cover_start", con)


def day_status(iv: pd.DataFrame, accounts: list[str], start: date, end: date,
               holidays: set) -> pd.DataFrame:
    """Trading-day × account status frame for [start, end]. `iv` = account_intervals(con)."""
    days = trading_days(start, end, holidays)
    out = pd.DataFrame(index=pd.Index(days, name="date"), columns=accounts, data="missing")
    for a in accounts:
        rows = iv[iv.client_id == a]
        full, partial = set(), set()
        for r in rows.itertuples():
            s0, e0 = date.fromisoformat(r.cover_start), date.fromisoformat(r.cover_end)
            for d in trading_days(s0, e0, holidays):
                if d == e0 and r.last_day_partial:
                    partial.add(d)
                else:
                    full.add(d)
        partial -= full  # a later/other export completing the day wins
        out.loc[out.index.isin(full), a] = "covered"
        out.loc[out.index.isin(partial), a] = "partial"
    return out


def combined_status(ds: pd.DataFrame) -> pd.Series:
    """Selection-level status: missing if any account missing; partial if any partial."""
    if ds.empty or ds.shape[1] == 0:
        return pd.Series(dtype=str)
    st = pd.Series("covered", index=ds.index)
    st[(ds == "partial").any(axis=1)] = "partial"
    st[(ds == "missing").any(axis=1)] = "missing"
    return st


def segments(status: pd.Series, value: str) -> list[tuple[date, date]]:
    """Contiguous runs of `value` in a day-status series."""
    out, cur = [], None
    for d, v in status.items():
        if v == value:
            cur = [d, d] if cur is None else [cur[0], d]
        elif cur is not None:
            out.append(tuple(cur)); cur = None
    if cur is not None:
        out.append(tuple(cur))
    return out


def coverage_notice(status: pd.Series, req_start: date, req_end: date) -> dict:
    """Requested vs supported period summary for the active selection."""
    obs = status[status != "missing"]
    if obs.empty:
        return dict(observed=None, missing=segments(status, "missing"), partial=[], complete=False,
                    req=(req_start, req_end), n_obs=0)
    first, last = obs.index.min(), obs.index.max()
    inner = status.loc[first:last]
    return dict(
        req=(req_start, req_end), observed=(first, last), n_obs=int(len(obs)),
        missing=segments(status, "missing"),
        internal_gaps=segments(inner, "missing"),
        partial=segments(status, "partial"),
        complete=bool((status == "covered").all()),
    )
