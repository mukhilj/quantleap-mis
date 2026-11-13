"""Drawdown depth AND duration on a sampled equity/P&L path.

Conventions (documented in the Methodology tab):
- The path starts with a baseline point (value before the first observation), so a loss
  on the very first observation is a drawdown from the baseline.
- Underwater   : value < running peak − tol.
- Recovered    : first observation with value ≥ peak − tol (equality within tolerance counts).
- New high     : value > running peak (strict). Flat-at-peak is recovered but not a new high.
- Durations    : calendar days between dates; "sessions" = count of observations (trading days
                 in the sampled series). Ongoing episodes are measured to the last observation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd


@dataclass
class DDInput:
    dates: list            # observation dates (baseline excluded)
    values: np.ndarray     # cumulative values at each observation (₹ P&L or index)
    baseline_value: float  # value before first observation
    baseline_date: date    # label date for the baseline point
    baseline_label: str = "start of observed history"
    prior_peak: float | None = None        # carry-forward mode: peak established before the window
    prior_peak_date: date | None = None


def underwater_series(inp: DDInput, tol: float) -> pd.DataFrame:
    """Per-observation peak, drawdown (≤0) and underwater flag."""
    v = np.asarray(inp.values, dtype=float)
    start_peak = inp.baseline_value if inp.prior_peak is None else max(inp.prior_peak, inp.baseline_value)
    peak = np.maximum.accumulate(np.concatenate([[start_peak], v]))[1:]
    dd = v - peak
    return pd.DataFrame({"value": v, "peak": peak, "drawdown": np.minimum(dd, 0.0),
                         "underwater": dd < -tol}, index=pd.Index(inp.dates, name="date"))


def episodes(inp: DDInput, tol: float, level: np.ndarray | None = None) -> pd.DataFrame:
    """Drawdown episodes. `level` (optional, same length as values) = equity level used for % depth
    (e.g. capital + cumulative P&L); % depth = depth / level at the peak."""
    v = np.asarray(inp.values, dtype=float)
    n = len(v)
    dates = list(inp.dates)
    if inp.prior_peak is not None and inp.prior_peak > inp.baseline_value:
        peak_val, peak_date, peak_pos = inp.prior_peak, inp.prior_peak_date, -1
        peak_note = "peak before window (carried forward)"
    else:
        peak_val, peak_date, peak_pos = inp.baseline_value, inp.baseline_date, -1
        peak_note = inp.baseline_label
    base_level = None if level is None else (level[0] - (v[0] - inp.baseline_value) if n else None)

    rows, cur = [], None
    for i in range(n):
        x = v[i]
        if x >= peak_val - tol:
            if cur is not None:  # recovery
                cur["recovery_date"], cur["recovery_pos"] = dates[i], i
                rows.append(cur); cur = None
            if x > peak_val:
                peak_val, peak_date, peak_pos, peak_note = x, dates[i], i, ""
        else:
            if cur is None:
                lvl = None
                if level is not None:
                    lvl = level[peak_pos] if peak_pos >= 0 else base_level
                cur = dict(peak_date=peak_date, peak_pos=peak_pos, peak_value=peak_val, peak_note=peak_note,
                           trough_date=dates[i], trough_pos=i, trough_value=x,
                           recovery_date=None, recovery_pos=None, peak_level=lvl)
            if x < cur["trough_value"]:
                cur["trough_date"], cur["trough_pos"], cur["trough_value"] = dates[i], i, x
    if cur is not None:
        rows.append(cur)

    last_date = dates[-1] if dates else None
    out = []
    for r in rows:
        rec = r["recovery_date"] is not None
        end_date = r["recovery_date"] if rec else last_date
        end_pos = r["recovery_pos"] if rec else n - 1
        depth = r["trough_value"] - r["peak_value"]
        out.append({
            "status": "Recovered" if rec else "Ongoing",
            "peak_date": r["peak_date"], "peak_note": r["peak_note"],
            "trough_date": r["trough_date"],
            "recovery_date": r["recovery_date"],
            "depth": depth,
            "depth_pct": (depth / r["peak_level"]) if r["peak_level"] not in (None, 0) else np.nan,
            "peak_to_trough_days": (r["trough_date"] - r["peak_date"]).days,
            "trough_to_recovery_days": (r["recovery_date"] - r["trough_date"]).days if rec else np.nan,
            "underwater_days": (end_date - r["peak_date"]).days,
            # sessions strictly below the peak (observations), through last obs if ongoing
            "underwater_sessions": (end_pos - r["peak_pos"] - (1 if rec else 0)),
            "peak_to_trough_sessions": r["trough_pos"] - r["peak_pos"],
            "trough_to_recovery_sessions": (r["recovery_pos"] - r["trough_pos"]) if rec else np.nan,
        })
    cols = ["status", "peak_date", "peak_note", "trough_date", "recovery_date", "depth", "depth_pct",
            "peak_to_trough_days", "trough_to_recovery_days", "underwater_days", "underwater_sessions",
            "peak_to_trough_sessions", "trough_to_recovery_sessions"]
    return pd.DataFrame(out, columns=cols)


def stagnation(inp: DDInput) -> dict:
    """Time without a NEW high (strict), including flat periods at the peak."""
    v = np.asarray(inp.values, dtype=float)
    if len(v) == 0:
        return dict(longest_days=np.nan, longest_sessions=np.nan, current_days=np.nan, current_sessions=np.nan,
                    longest_from=None, longest_to=None)
    peak = inp.baseline_value if inp.prior_peak is None else max(inp.prior_peak, inp.baseline_value)
    last_hi_date, last_hi_pos = (inp.prior_peak_date if (inp.prior_peak is not None and inp.prior_peak > inp.baseline_value)
                                 else inp.baseline_date), -1
    best = (0, 0, last_hi_date, last_hi_date)
    for i, x in enumerate(v):
        if x > peak:
            d = inp.dates[i]
            gap_days, gap_sess = (d - last_hi_date).days, i - last_hi_pos - 1
            if gap_days > best[0]:
                best = (gap_days, gap_sess, last_hi_date, d)
            peak, last_hi_date, last_hi_pos = x, d, i
    cur_days = (inp.dates[-1] - last_hi_date).days
    cur_sess = len(v) - 1 - last_hi_pos
    if cur_days > best[0]:
        best = (cur_days, cur_sess, last_hi_date, None)
    return dict(longest_days=best[0], longest_sessions=best[1], longest_from=best[2], longest_to=best[3],
                current_days=cur_days, current_sessions=cur_sess, last_new_high=last_hi_date)


def summarise(ep: pd.DataFrame, uw: pd.DataFrame) -> dict:
    """Deepest, longest, deepest-recovery and current drawdown — possibly different episodes."""
    n = len(uw)
    res = dict(n_episodes=len(ep), pct_time_underwater=(uw["underwater"].sum() / n) if n else np.nan,
               max_dd=np.nan, deepest=None, longest=None, current=None)
    if ep.empty:
        res["max_dd"] = 0.0 if n else np.nan
        return res
    deepest = ep.loc[ep["depth"].idxmin()]
    longest = ep.loc[ep["underwater_days"].idxmax()]
    res.update(max_dd=float(deepest["depth"]), deepest=deepest.to_dict(), longest=longest.to_dict())
    last = ep.iloc[-1]
    if last["status"] == "Ongoing":
        cur = last.to_dict()
        cur["current_depth"] = float(uw["drawdown"].iloc[-1])
        res["current"] = cur
    return res
