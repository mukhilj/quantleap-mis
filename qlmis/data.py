"""Load the ledger into analysis-ready frames and apply sidebar selections."""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from .config import Settings, load_aliases
from .charges import estimate_charges

SYMBOL_RE = re.compile(
    r"^(?P<und>BANKNIFTY|FINNIFTY|MIDCPNIFTY|NIFTYNXT50|NIFTY|SENSEX|BANKEX)"
    r"(?P<yy>\d{2})(?P<exp>JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC|[1-9OND]\d{2})"
    r"(?P<strike>\d+(?:\.\d+)?)(?P<type>CE|PE)$")


def parse_symbol(sym: str) -> tuple[str, float | None, str]:
    m = SYMBOL_RE.match(sym or "")
    if not m:
        return (sym or "?"), None, "?"
    return m["und"], float(m["strike"]), m["type"]


def load_legs(con: sqlite3.Connection, s: Settings, holidays: set) -> pd.DataFrame:
    """All ledger legs (including cancelled), enriched. Filter with `filled` for performance."""
    df = pd.read_sql_query(
        "SELECT l.*, f.original_name AS source_file FROM legs l "
        "LEFT JOIN source_files f ON f.file_id = l.first_file_id", con)
    if df.empty:
        return df
    df["filled"] = df["entry_status"].eq("COMPLETE")
    parsed = df["symbol"].map(parse_symbol)
    df["instrument"] = parsed.map(lambda t: t[0])
    df["strike"] = parsed.map(lambda t: t[1])
    df["option_type"] = parsed.map(lambda t: t[2])
    df["close_date"] = pd.to_datetime(df["closed_date"]).dt.date
    df["exec_date"] = pd.to_datetime(df["execution_date"]).dt.date
    ts = pd.to_datetime(df["entry_filled_at"].replace("", None), utc=True).dt.tz_convert("Asia/Kolkata")
    df["entry_ts"] = ts
    df["entry_date"] = ts.dt.date
    df["entry_time"] = ts.dt.strftime("%H:%M")
    df["entry_hour"] = ts.dt.hour
    # Holding period: entry fill date -> close date (no exit timestamp exists in the source).
    hol = np.array(sorted(holidays), dtype="datetime64[D]")
    ed = df["entry_date"].where(df["filled"])
    mask = ed.notna() & df["close_date"].notna()
    df["hold_cal_days"] = np.nan
    df["hold_sessions"] = np.nan
    df.loc[mask, "hold_cal_days"] = [(c - e).days for c, e in zip(df.loc[mask, "close_date"], ed[mask])]
    df.loc[mask, "hold_sessions"] = np.busday_count(
        np.array(ed[mask].tolist(), dtype="datetime64[D]"),
        np.array(df.loc[mask, "close_date"].tolist(), dtype="datetime64[D]"), holidays=hol)
    df["hold_band"] = pd.cut(df["hold_sessions"], [-1, 0, 1, 3, 1e9],
                             labels=["Intraday (same session)", "1 session", "2–3 sessions", "4+ sessions"])
    # Identity of a system = (account, strategy_id). Names are not unique.
    df["system_key"] = df["client_id"] + "|" + df["strategy_id"]
    al = load_aliases(s)
    al["system_key"] = al["client_id"] + "|" + al["strategy_id"]
    names = dict(zip(al["system_key"], al["display_name"].replace("", None)))
    logical = dict(zip(al["system_key"], al["logical_strategy"]))
    df["display_name"] = df["system_key"].map(names).fillna(df["strategy_name"])
    df["logical_strategy"] = df["system_key"].map(logical).fillna("")
    acct = s.accounts
    df["account_label"] = df["client_id"].map(lambda c: acct.get(c, c))
    df["system_label"] = df["account_label"] + " · " + df["display_name"] + " [" + df["basket_name"] + "]"
    # Derived strategy run: all legs a strategy armed on one execution date (not source-provided).
    df["run_key"] = df["system_key"] + "|" + df["execution_date"]
    df["provenance"] = df["source_file"].fillna("?") + " : line " + df["first_row"].astype("Int64").astype(str)
    df = df.join(estimate_charges(df, s.charges))
    df["net_est_pnl"] = df["pnl"] - df["est_charges"]
    df["entry_slippage_inr"] = np.where(
        df["filled"],
        (df["expected_entry_price"] - df["average_entry_price"]) * df["quantity"]
        * np.where(df["action"].eq("BUY"), 1, -1),
        np.nan)  # positive = better fill than expected price
    return df


def systems_table(legs: pd.DataFrame) -> pd.DataFrame:
    return (legs.groupby(["system_key", "client_id", "account_label", "strategy_id", "display_name",
                          "basket_name", "system_label", "logical_strategy"], as_index=False)
            .agg(filled_legs=("filled", "sum"), instruments=("instrument", lambda x: ", ".join(sorted(set(x))))))


def load_baskets(con: sqlite3.Connection) -> dict[str, set[str]]:
    rows = con.execute("SELECT b.name, m.client_id, m.strategy_id FROM baskets b "
                       "LEFT JOIN basket_members m ON m.basket_id=b.basket_id ORDER BY b.name").fetchall()
    out: dict[str, set[str]] = {}
    for r in rows:
        out.setdefault(r[0], set())
        if r[1]:
            out[r[0]].add(f"{r[1]}|{r[2]}")
    return out


def save_basket(con: sqlite3.Connection, name: str, members: list[str], description: str = "") -> None:
    """Create or replace a user basket. members = system_keys 'client_id|strategy_id'."""
    from .ingest import now_ist
    name = name.strip()
    if not name:
        raise ValueError("Basket name required")
    con.execute("BEGIN IMMEDIATE")
    try:
        b = con.execute("SELECT basket_id FROM baskets WHERE name=?", (name,)).fetchone()
        if b is None:
            bid = con.execute("INSERT INTO baskets(name,description,origin,created_at,updated_at) VALUES(?,?,?,?,?)",
                              (name, description, "user", now_ist(), now_ist())).lastrowid
        else:
            bid = b[0]
            # Editing a seeded basket turns it into a user basket so imports stop extending it.
            con.execute("UPDATE baskets SET description=?, origin='user', updated_at=? WHERE basket_id=?",
                        (description, now_ist(), bid))
            con.execute("DELETE FROM basket_members WHERE basket_id=?", (bid,))
        for k in members:
            c, sid = k.split("|", 1)
            con.execute("INSERT INTO basket_members VALUES(?,?,?)", (bid, c, sid))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def delete_basket(con: sqlite3.Connection, name: str) -> None:
    con.execute("DELETE FROM baskets WHERE name=?", (name,))


@dataclass
class Selection:
    start: date
    end: date
    accounts: list[str] = field(default_factory=list)     # client_ids; empty = all
    systems: list[str] = field(default_factory=list)      # system_keys; empty = all
    baskets: list[str] = field(default_factory=list)      # basket names; empty = no basket constraint
    instruments: list[str] = field(default_factory=list)  # empty = all
    cost_basis: str = "gross"                             # gross | net_est
    unit: str = "leg"                                     # leg | run


def selected_systems(all_systems: set[str], sel: Selection, baskets: dict[str, set[str]]) -> set[str]:
    """Universe = union of chosen baskets (or everything); then narrowed by accounts and systems.

    A system in several selected baskets is included once (set semantics).
    """
    uni = set().union(*(baskets.get(b, set()) for b in sel.baskets)) if sel.baskets else set(all_systems)
    uni &= all_systems
    if sel.accounts:
        uni = {k for k in uni if k.split("|", 1)[0] in sel.accounts}
    if sel.systems:
        uni &= set(sel.systems)
    return uni


def apply_selection(legs: pd.DataFrame, sel: Selection, baskets: dict[str, set[str]]) -> tuple[pd.DataFrame, set[str]]:
    """Filled legs within the selection, by close date. Returns (legs, system_keys)."""
    keys = selected_systems(set(legs["system_key"].unique()), sel, baskets)
    m = legs["filled"] & legs["system_key"].isin(keys)
    m &= (legs["close_date"] >= sel.start) & (legs["close_date"] <= sel.end)
    if sel.instruments:
        m &= legs["instrument"].isin(sel.instruments)
    out = legs.loc[m].copy()
    out["pnl_basis"] = out["pnl"] if sel.cost_basis == "gross" else out["net_est_pnl"]
    return out, keys


def runs_from_legs(legs: pd.DataFrame) -> pd.DataFrame:
    """Aggregate filled legs into derived strategy runs. Run P&L is realised on its last close date."""
    if legs.empty:
        return pd.DataFrame(columns=["run_key", "pnl_basis", "close_date"])
    g = legs.groupby("run_key")
    return pd.DataFrame({
        "system_key": g["system_key"].first(), "system_label": g["system_label"].first(),
        "client_id": g["client_id"].first(), "execution_date": g["execution_date"].first(),
        "legs": g.size(), "first_entry": g["entry_ts"].min(), "close_date": g["close_date"].max(),
        "pnl": g["pnl"].sum(), "pnl_basis": g["pnl_basis"].sum(), "est_charges": g["est_charges"].sum(),
        "instrument": g["instrument"].first(),
        "hold_sessions": g["hold_sessions"].max(),
    }).reset_index()
