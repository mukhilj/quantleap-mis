"""Selection -> one analysis context. Screen and exports both read from this object."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd

from . import coverage as C
from . import drawdown as D
from . import metrics as M
from .data import Selection, apply_selection, runs_from_legs


@dataclass
class Context:
    sel: Selection
    legs: pd.DataFrame                 # filled legs in selection
    units: pd.DataFrame                # legs or derived runs (per sel.unit), chronological
    keys: set
    accounts: list
    day_status: pd.DataFrame
    status: pd.Series
    daily: pd.DataFrame
    obs: pd.DataFrame
    notice: dict
    dd_input: D.DDInput | None
    episodes: pd.DataFrame
    underwater: pd.DataFrame
    dd_summary: dict
    stagnation: dict
    unit_stats: dict
    day_stats: dict
    whole_accounts: bool
    capital_path: pd.DataFrame | None
    returns: dict
    capital_reason: str
    labels: dict = field(default_factory=dict)


def build(legs_all: pd.DataFrame, sel: Selection, baskets: dict, intervals: pd.DataFrame,
          holidays: set, capital: pd.DataFrame, calc: dict, dd_mode: str = "reset") -> Context:
    tol, be = float(calc["drawdown_tolerance_inr"]), float(calc["breakeven_tolerance_inr"])
    L, keys = apply_selection(legs_all, sel, baskets)
    accounts = sorted({k.split("|", 1)[0] for k in keys})
    ds = C.day_status(intervals, accounts, sel.start, sel.end, holidays)
    status = C.combined_status(ds)
    daily = M.daily_series(L, status)
    obs = M.observed(daily)
    notice = C.coverage_notice(status, sel.start, sel.end)

    # Units for outcome statistics
    L = L.sort_values(["close_date", "entry_ts"])
    if sel.unit == "run":
        units = runs_from_legs(L).sort_values(["close_date", "first_entry"])
    else:
        units = L
    unit_stats = M.outcome_stats(units["pnl_basis"] if not units.empty else pd.Series(dtype=float), be)
    day_stats = M.outcome_stats(obs.loc[obs["n_closes"] > 0, "pnl"], be)

    # Prior history (carry-forward drawdown baseline)
    prior = None
    if dd_mode == "carry" and not intervals.empty and accounts:
        earliest = min(date.fromisoformat(x) for x in intervals[intervals.client_id.isin(accounts)].cover_start)
        if earliest < sel.start:
            sel_p = Selection(**{**sel.__dict__, "start": earliest, "end": sel.start - timedelta(days=1)})
            Lp, _ = apply_selection(legs_all, sel_p, baskets)
            stp = C.combined_status(C.day_status(intervals, accounts, sel_p.start, sel_p.end, holidays))
            prior = M.observed(M.daily_series(Lp, stp))

    # Whole-account test: capital belongs to accounts, not strategies.
    all_keys_of_accts = set(legs_all.loc[legs_all["client_id"].isin(accounts), "system_key"])
    whole = bool(accounts) and keys == all_keys_of_accts and not sel.instruments
    cp, reason = None, ""
    if not whole:
        reason = ("Selection is a subset of an account's systems/instruments; capital is recorded per account, "
                  "so % returns are not attributable.")
    elif obs.empty:
        reason = "No observed days in range."
    else:
        cp = M.capital_path(obs, capital, accounts)
        if cp is None:
            reason = "No capital recorded for the selected account(s) on or before the first observed day."
    rets = M.return_metrics(cp, calc)

    if obs.empty:
        empty = D.DDInput([], np.array([]), 0.0, sel.start)
        inp, ep, uw, stg = None, D.episodes(empty, tol), D.underwater_series(empty, tol), {}
        sm = D.summarise(ep, uw)
    else:
        inp = M.dd_input_from_daily(obs, prior=prior, mode=dd_mode)
        # % depth of ₹ episodes = depth / (capital + cumulative P&L) at the peak; reset mode only.
        level = cp["equity"].to_numpy() if (cp is not None and dd_mode == "reset") else None
        ep = D.episodes(inp, tol, level=level)
        uw = D.underwater_series(inp, tol)
        sm = D.summarise(ep, uw)
        stg = D.stagnation(inp)

    labels = dict(zip(legs_all["system_key"], legs_all["system_label"]))
    return Context(sel=sel, legs=L, units=units, keys=keys, accounts=accounts, day_status=ds, status=status,
                   daily=daily, obs=obs, notice=notice, dd_input=inp, episodes=ep, underwater=uw, dd_summary=sm,
                   stagnation=stg, unit_stats=unit_stats, day_stats=day_stats, whole_accounts=whole,
                   capital_path=cp, returns=rets, capital_reason=reason, labels=labels)


def headline_table(ctx: Context) -> pd.DataFrame:
    """Numeric summary (one row per metric) used by both the Summary tab export and the XLSX."""
    u, d, r, sm, stg = ctx.unit_stats, ctx.day_stats, ctx.returns, ctx.dd_summary, ctx.stagnation
    obs = ctx.notice.get("observed")
    unit = "derived strategy runs" if ctx.sel.unit == "run" else "legs (filled positions)"
    basis = "gross (source)" if ctx.sel.cost_basis == "gross" else "estimated net (hypothetical charges)"
    deep, lng, cur = sm.get("deepest"), sm.get("longest"), sm.get("current")
    monthly = M.period_table(ctx.obs, "M", ctx.daily)
    m_done = monthly[monthly["completeness"].isin(["complete", "partial (range edge)", "partial (incomplete day)"])] \
        if not monthly.empty else monthly
    rows = [
        ("Observed period start", obs[0] if obs else None, "date", ""),
        ("Observed period end", obs[1] if obs else None, "date", ""),
        ("Observed trading days", ctx.notice.get("n_obs", 0), "days", ""),
        ("Cost basis", basis, "text", ""),
        ("Reporting unit", unit, "text", ""),
        ("Total P&L", u["total"], "INR", basis),
        ("Estimated charges in selection", float(ctx.legs["est_charges"].sum()) if not ctx.legs.empty else 0.0, "INR", "hypothetical"),
        ("Max drawdown (₹)", sm.get("max_dd"), "INR", "realised, daily close"),
        ("Deepest DD peak date", deep["peak_date"] if deep else None, "date", ""),
        ("Deepest DD trough date", deep["trough_date"] if deep else None, "date", ""),
        ("Deepest DD recovery date", deep["recovery_date"] if deep else None, "date", "blank = not recovered"),
        ("Deepest DD recovery duration (days)", deep["trough_to_recovery_days"] if deep else None, "days", ""),
        ("Longest time underwater (days)", lng["underwater_days"] if lng else 0, "calendar days", ""),
        ("Longest time underwater (sessions)", lng["underwater_sessions"] if lng else 0, "sessions", ""),
        ("Longest underwater depth (₹)", lng["depth"] if lng else None, "INR", ""),
        ("Current drawdown (₹)", cur["current_depth"] if cur else 0.0, "INR", ""),
        ("Current drawdown elapsed (days)", cur["underwater_days"] if cur else 0, "calendar days", ""),
        ("Longest without new high (days)", stg.get("longest_days"), "calendar days", ""),
        ("% of observed sessions underwater", sm.get("pct_time_underwater"), "ratio", ""),
        ("Count", u["n"], unit, ""),
        ("Wins", u["wins"], unit, ""), ("Losses", u["losses"], unit, ""), ("Breakevens", u["breakevens"], unit, ""),
        ("Win rate (wins/all)", u["win_rate"], "ratio", ""),
        ("Average win", u["avg_win"], "INR", ""), ("Average loss", u["avg_loss"], "INR", ""),
        ("Median win", u["median_win"], "INR", ""), ("Median loss", u["median_loss"], "INR", ""),
        ("Payoff ratio", u["payoff"], "x", "avg win / |avg loss|"),
        ("Profit factor", u["profit_factor"], "x", "gross wins / |gross losses|"),
        ("Expectancy", u["expectancy"], "INR per unit", ""),
        ("Best", u["best"], "INR", unit), ("Worst", u["worst"], "INR", unit),
        ("Max winning streak", u["max_win_streak"], unit, ""), ("Max losing streak", u["max_loss_streak"], unit, ""),
        ("Best day", d["best"], "INR", ""), ("Worst day", d["worst"], "INR", ""),
        ("Profitable days %", d["win_rate"], "ratio", "of days with closes"),
        ("Max winning day streak", d["max_win_streak"], "days", ""), ("Max losing day streak", d["max_loss_streak"], "days", ""),
        ("Profitable months", int((m_done["pnl"] > 0).sum()) if not m_done.empty else 0, "months", "incl. partial months"),
        ("Months observed", len(m_done), "months", ""),
        ("Recovery factor", (u["total"] / abs(sm["max_dd"])) if sm.get("max_dd") else None, "x", "net P&L / |max ₹ DD|"),
        ("Total return (TWR)", r["total_return"], "ratio", ""), ("CAGR", r["cagr"], "ratio", ""),
        ("Annualised volatility", r["vol"], "ratio", ""), ("Sharpe", r["sharpe"], "x", ""),
        ("Sortino", r["sortino"], "x", ""), ("Max drawdown %", r["max_dd_pct"], "ratio", "on TWR index"),
        ("Calmar-style", r["calmar"], "x", "CAGR / |max DD %|"), ("Money-weighted return (XIRR)", r["mwr"], "ratio", ""),
    ]
    return pd.DataFrame(rows, columns=["metric", "value", "unit", "note"])
