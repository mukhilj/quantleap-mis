"""QuantLeap Performance MIS — local Streamlit dashboard.

Launch:  python -m streamlit run app.py      (or double-click run_dashboard.bat)
"""
from __future__ import annotations

import io
import json
import math
import sqlite3
from datetime import date, timedelta

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from qlmis import coverage as C
from qlmis import metrics as M
from qlmis import portfolio as P
from qlmis import report as R
from qlmis.config import load_aliases, load_capital, load_holidays, load_settings, save_capital, CAPITAL_COLUMNS
from qlmis.coverage import account_intervals
from qlmis.data import Selection, delete_basket, load_baskets, load_legs, save_basket, systems_table
from qlmis.db import connect
from qlmis.fmt import inr, lakh, num, pct
from qlmis.ingest import resolve_review, run_import

st.set_page_config(page_title="QuantLeap MIS", page_icon="📈", layout="wide")

GAIN, LOSS, LINE, MUTED = "#1a9850", "#d73027", "#2c6fbb", "#9aa0a6"
S = load_settings()


# ---------------------------------------------------------------- cached loading
def _mtimes() -> tuple:
    files = [S.database, S.capital_file, S.aliases_file, S.holidays_file, S.root / "config" / "settings.toml"]
    return tuple(f.stat().st_mtime if f.exists() else 0 for f in files)


@st.cache_data(show_spinner="Loading ledger…")
def load_all(_stamp: tuple):
    con = connect(S.database)
    hol = load_holidays(S)
    legs = load_legs(con, S, hol)
    baskets = load_baskets(con)
    iv = account_intervals(con)
    files = pd.read_sql_query("SELECT * FROM source_files ORDER BY file_id", con)
    runs = pd.read_sql_query("SELECT * FROM import_runs ORDER BY run_id DESC", con)
    review = pd.read_sql_query("SELECT * FROM review_queue ORDER BY review_id DESC", con)
    rollups = pd.read_sql_query("SELECT * FROM rollups ORDER BY snapshot_date DESC", con)
    sightings = pd.read_sql_query("SELECT s.file_id, l.client_id, l.strategy_id, l.pnl, l.entry_status "
                                  "FROM sightings s JOIN legs l ON l.leg_key = s.leg_key", con)
    revisions = pd.read_sql_query("SELECT change_type, COUNT(*) n FROM leg_revisions GROUP BY change_type", con)
    con.close()
    return legs, baskets, iv, hol, files, runs, review, rollups, sightings, revisions


def fresh():
    st.cache_data.clear()


legs_all, BASKETS, IV, HOL, FILES, RUNS, REVIEW, ROLLUPS, SIGHT, REVS = load_all(_mtimes())
CAPITAL = load_capital(S)

if legs_all.empty:
    st.title("QuantLeap Performance MIS")
    st.info("The ledger is empty. Put QuantLead exports in the project folder or `inbox/`, then import.")
    if st.button("Import now", type="primary"):
        rep = run_import(S)
        st.write(rep.to_dict())
        fresh(); st.rerun()
    st.stop()

SYS = systems_table(legs_all)
LABEL = dict(zip(SYS.system_key, SYS.system_label))
_short = SYS.account_label + " · " + SYS.display_name
_dup = _short.duplicated(keep=False)  # e.g. two different strategies sharing one name in an account
_short[_dup] = _short[_dup] + " [" + SYS.basket_name[_dup] + "]"
SHORT = dict(zip(SYS.system_key, _short))
ACCOUNTS = sorted(legs_all.client_id.unique())
INSTR = sorted(legs_all.instrument.unique())
first_cov = min(date.fromisoformat(x) for x in IV.cover_start) if not IV.empty else legs_all.close_date.min()
last_cov = max(date.fromisoformat(x) for x in IV.cover_end) if not IV.empty else legs_all.close_date.max()

# ---------------------------------------------------------------- sidebar
DEFAULTS = dict(f_range=(first_cov, last_cov), f_baskets=[], f_accounts=[], f_systems=[], f_instr=[],
                f_cost="Gross (actual)", f_unit="Legs (filled positions)", f_ddmode="Reset at range start")
for k, v in DEFAULTS.items():
    st.session_state.setdefault(k, v)

with st.sidebar:
    st.header("Filters")
    st.date_input("Date range (by close date)", key="f_range", min_value=date(2020, 1, 1), max_value=date(2035, 12, 31),
                  help="Detailed history is available only for covered days; anything else is shown as missing, never as zero.")
    st.multiselect("Named baskets", sorted(BASKETS), key="f_baskets",
                   help="Union of the chosen baskets. A system in several baskets is counted once. Empty = all systems.")
    st.multiselect("Accounts", ACCOUNTS, key="f_accounts", format_func=lambda a: S.accounts.get(a, a))
    # System options narrow with basket/account choices so selections stay predictable.
    sys_opts = SYS.copy()
    if st.session_state.f_baskets:
        allowed = set().union(*(BASKETS[b] for b in st.session_state.f_baskets))
        sys_opts = sys_opts[sys_opts.system_key.isin(allowed)]
    if st.session_state.f_accounts:
        sys_opts = sys_opts[sys_opts.client_id.isin(st.session_state.f_accounts)]
    st.session_state.f_systems = [k for k in st.session_state.f_systems if k in set(sys_opts.system_key)]
    st.multiselect("Systems", list(sys_opts.system_key), key="f_systems", format_func=lambda k: LABEL.get(k, k))
    st.multiselect("Instruments", INSTR, key="f_instr")
    st.radio("Cost basis", ["Gross (actual)", "Estimated net (hypothetical)"], key="f_cost",
             help="Source P&L is gross. 'Estimated net' subtracts modelled charges from config/settings.toml [charges].")
    st.radio("Reporting unit", ["Legs (filled positions)", "Derived runs"], key="f_unit",
             help="Leg = one option position (entry→exit). Derived run = all legs a strategy armed on one execution date "
                  "(derived, not provided by QuantLead).")
    st.radio("Drawdown baseline", ["Reset at range start", "Carry prior peak"], key="f_ddmode",
             help="Reset: drawdowns measured from ₹0 at the range start. Carry: uses earlier observed history, so an "
                  "episode that began before the range keeps its original peak.")
    c1, c2 = st.columns(2)
    if c1.button("Reset filters", width="stretch"):
        for k in DEFAULTS:
            st.session_state.pop(k, None)
        st.rerun()
    if c2.button("Import / refresh", type="primary", width="stretch"):
        with st.spinner("Importing…"):
            rep = run_import(S)
        st.session_state["last_import"] = rep.to_dict()
        fresh(); st.rerun()
    if "last_import" in st.session_state:
        li = st.session_state["last_import"]
        c = li["counts"]
        msg = (f"Import #{li['run_id']} {li['status']}: {c['new']} new, {c['duplicate']} duplicates, "
               f"{c['update']} updates, {c['conflict']} conflicts, {c['invalid']} invalid. "
               f"Legs {li['legs_before']}→{li['legs_after']}.")
        (st.warning if c["conflict"] or c["invalid"] or li["status"] == "failed" else st.success)(msg)

rng = st.session_state.f_range
if not isinstance(rng, (tuple, list)) or len(rng) != 2:
    st.info("Pick an end date for the range."); st.stop()
SEL = Selection(start=rng[0], end=rng[1], accounts=st.session_state.f_accounts, systems=st.session_state.f_systems,
                baskets=st.session_state.f_baskets, instruments=st.session_state.f_instr,
                cost_basis="gross" if st.session_state.f_cost.startswith("Gross") else "net_est",
                unit="run" if st.session_state.f_unit.startswith("Derived") else "leg")
DDMODE = "carry" if st.session_state.f_ddmode.startswith("Carry") else "reset"
CALC = S.calc
TOL, BE = float(CALC["drawdown_tolerance_inr"]), float(CALC["breakeven_tolerance_inr"])
ctx = R.build(legs_all, SEL, BASKETS, IV, HOL, CAPITAL, CALC, DDMODE)
BASIS = "gross" if SEL.cost_basis == "gross" else "est. net"
UNIT = "run" if SEL.unit == "run" else "leg"


# ---------------------------------------------------------------- helpers
def fmt_date(d):
    return "—" if d is None or (isinstance(d, float) and math.isnan(d)) or pd.isna(d) else pd.Timestamp(d).strftime("%d %b %Y")


def money_cols(df: pd.DataFrame, cols: list[str]) -> dict:
    return {c: st.column_config.NumberColumn(c, format="₹ %.0f") for c in cols if c in df}


def download(df: pd.DataFrame, name: str, label: str = "Download CSV"):
    st.download_button(label, df.to_csv(index=isinstance(df.index, pd.MultiIndex) or df.index.name is not None).encode("utf-8-sig"),
                       file_name=f"{name}.csv", mime="text/csv", key=f"dl_{name}")


def ratio(x, d=2):
    if x is None:
        return "n/a"
    if isinstance(x, float) and math.isinf(x):
        return "∞ (no losses)"
    return num(x, d)


def sel_caption() -> str:
    parts = []
    parts.append(f"Baskets: {', '.join(SEL.baskets)}" if SEL.baskets else "All systems")
    if SEL.accounts:
        parts.append(f"Accounts: {', '.join(SEL.accounts)}")
    if SEL.systems:
        parts.append(f"{len(SEL.systems)} system(s) picked")
    if SEL.instruments:
        parts.append(f"Instruments: {', '.join(SEL.instruments)}")
    obs = ctx.notice.get("observed")
    eff = f"{fmt_date(obs[0])} → {fmt_date(obs[1])} ({ctx.notice['n_obs']} trading days)" if obs else "no observed days"
    return (f"**Selection:** {' · '.join(parts)} — {len(ctx.keys)} systems in {len(ctx.accounts)} account(s). "
            f"**Effective coverage:** {eff}. **Basis:** {BASIS}, realised, by close date. **Unit:** {UNIT}s.")


def coverage_banner():
    n = ctx.notice
    msgs = []
    if n.get("observed") is None:
        st.error("No detailed history exists for this selection and date range.")
        return
    o0, o1 = n["observed"]
    if SEL.start < o0:
        msgs.append(f"Requested **{fmt_date(SEL.start)} → {fmt_date(SEL.end)}**, but detailed history for this selection "
                    f"starts **{fmt_date(o0)}** (QuantLead keeps closed positions ~{S.retention_days} days). "
                    f"{fmt_date(SEL.start)} → {fmt_date(o0 - timedelta(days=1))} is **missing — not zero**.")
    if n.get("internal_gaps"):
        g = ", ".join(f"{fmt_date(a)}–{fmt_date(b)}" for a, b in n["internal_gaps"])
        msgs.append(f"Missing days inside the observed window: {g}. Curves break there; durations spanning them are uncertain.")
    if n.get("partial"):
        g = ", ".join(fmt_date(a) for a, _ in n["partial"])
        msgs.append(f"Partial day(s): {g} (export taken mid-session; later closes not yet imported).")
    if SEL.end > o1:
        msgs.append(f"No data after {fmt_date(o1)}.")
    if msgs:
        msgs.append("Affected: every trade-derived statistic, curve, drawdown and annualised figure covers only the "
                    "observed window. Drawdowns are measured over observed history only — an earlier peak may exist.")
        st.warning("  \n".join(msgs), icon="⚠️")


_CHART_N = [0]


def pchart(container, fig):
    """plotly_chart with a unique key (several charts share identical specs)."""
    _CHART_N[0] += 1
    container.plotly_chart(fig, width="stretch", key=f"chart_{_CHART_N[0]}")


def metric(col, label, value, help_=None, delta=None):
    col.metric(label, value, delta=delta, help=help_, delta_color="off")


def plot_layout(fig, h=320, ytitle=None):
    fig.update_layout(height=h, margin=dict(l=10, r=10, t=30, b=10), hovermode="x unified",
                      legend=dict(orientation="h", y=1.08), yaxis_title=ytitle)
    return fig


# ---------------------------------------------------------------- header
st.title("QuantLeap Performance MIS")
st.caption(sel_caption())
coverage_banner()
if ctx.legs.empty:
    st.info("No filled positions closed in this selection.")

tabs = st.tabs(["📋 Summary", "📉 Equity & Drawdowns", "🧩 Systems & Baskets", "📅 Calendar & Consistency",
                "🔎 Trades & Duration", "🗂 Data & Methodology"])
u, dstat, sm, rets, stg = ctx.unit_stats, ctx.day_stats, ctx.dd_summary, ctx.returns, ctx.stagnation
deep, longest, cur = sm.get("deepest"), sm.get("longest"), sm.get("current")


def equity_fig(show_accounts=False):
    obs = ctx.daily.copy()
    cum = obs["pnl"].where(obs["status"] != "missing").cumsum()
    # break the line across missing days instead of drawing a flat line
    cum[obs["status"] == "missing"] = np.nan
    fig = go.Figure()
    if ctx.dd_input is not None:
        base = pd.Series([ctx.dd_input.baseline_value], index=[ctx.dd_input.baseline_date])
        y = pd.concat([base, pd.Series(ctx.dd_input.values, index=ctx.dd_input.dates)])
        y = y.reindex(sorted(set(y.index) | set(obs.index)))
        y[[d for d in obs.index if obs.at[d, "status"] == "missing"]] = np.nan
        fig.add_scatter(x=y.index, y=y.values, mode="lines", name=f"Cumulative realised P&L ({BASIS})",
                        line=dict(color=LINE, width=2), connectgaps=False)
        pk = pd.Series(ctx.underwater["peak"].values, index=ctx.underwater.index)
        fig.add_scatter(x=pk.index, y=pk.values, mode="lines", name="Running peak", line=dict(color=MUTED, dash="dot", width=1))
    if show_accounts and len(ctx.accounts) > 1:
        for a in ctx.accounts:
            la = ctx.legs[ctx.legs.client_id == a].groupby("close_date")["pnl_basis"].sum()
            s_ = la.reindex(ctx.obs.index).fillna(0).cumsum()
            fig.add_scatter(x=s_.index, y=s_.values, mode="lines", name=a, line=dict(width=1))
    for a, _ in ctx.notice.get("partial", []):
        fig.add_vline(x=a, line_dash="dot", line_color=MUTED)
    return plot_layout(fig, ytitle="₹ (realised, daily close)")


# ================================================================= TAB 1 SUMMARY
with tabs[0]:
    c = st.columns(4)
    metric(c[0], f"Total P&L ({BASIS})", inr(u["total"]),
           "Sum of realised P&L of filled legs closed in the observed window. Gross = source figure (no charges).")
    metric(c[1], "Max drawdown (₹)", inr(sm.get("max_dd")),
           f"Deepest peak-to-trough fall of cumulative realised P&L at daily close. "
           f"{('Peak ' + fmt_date(deep['peak_date']) + ' → trough ' + fmt_date(deep['trough_date'])) if deep else ''}")
    metric(c[2], "Longest time underwater", f"{longest['underwater_days']} days" if longest else "0 days",
           "Longest calendar span from a peak until equity got back to it (or until the last observation if not yet "
           "recovered). Can be a different episode from the deepest one.",
           delta=(f"{longest['underwater_sessions']} sessions · depth {inr(longest['depth'])}" if longest else None))
    metric(c[3], "Current drawdown", inr(cur["current_depth"]) if cur else "None (at peak)",
           "Distance below the running peak at the last observation, and time since that peak.",
           delta=(f"{cur['underwater_days']} days since peak {fmt_date(cur['peak_date'])}" if cur else None))
    c = st.columns(4)
    metric(c[0], f"Win rate ({UNIT}s)", pct(u["win_rate"]),
           f"Wins ÷ all {u['n']} {UNIT}s (breakevens: {u['breakevens']}, counted in the denominator). "
           f"Excluding breakevens: {pct(u['win_rate_decided'])}.")
    metric(c[1], "Avg win / avg loss", f"{inr(u['avg_win'])} / {inr(u['avg_loss'])}",
           f"Mean winning and losing {UNIT}. Payoff ratio = {ratio(u['payoff'])}.")
    metric(c[2], "Profit factor", ratio(u["profit_factor"]), "Gross winning P&L ÷ |gross losing P&L|.")
    metric(c[3], f"Expectancy per {UNIT}", inr(u["expectancy"]),
           "P(win) × avg win − P(loss) × |avg loss|, with probabilities over all outcomes incl. breakevens.")

    pchart(st, equity_fig())
    st.caption("Cumulative **realised** P&L sampled at daily close (by close date). Intratrade and intraday swings are "
               "not in the source and are not shown. Gaps = missing days. Dotted line = running peak.")

    left, right = st.columns([1, 1])
    with left:
        st.subheader("Statistics", divider="gray")
        monthly = M.period_table(ctx.obs, "M", ctx.daily)
        mok = monthly[monthly.completeness != "missing"] if not monthly.empty else monthly
        rows = [
            (f"Completed {UNIT}s", f"{u['n']}  (W {u['wins']} / L {u['losses']} / BE {u['breakevens']})"),
            ("Payoff ratio", ratio(u["payoff"])),
            ("Median win / median loss", f"{inr(u['median_win'])} / {inr(u['median_loss'])}"),
            (f"Best / worst {UNIT}", f"{inr(u['best'])} / {inr(u['worst'])}"),
            ("Best / worst day", f"{inr(dstat['best'])} / {inr(dstat['worst'])}"),
            (f"Longest win / loss streak ({UNIT}s)", f"{u['max_win_streak']} / {u['max_loss_streak']}"),
            ("Longest win / loss streak (days)", f"{dstat['max_win_streak']} / {dstat['max_loss_streak']}"),
            ("Profitable days", f"{pct(dstat['win_rate'])} of {dstat['n']} days with closes"),
            ("Profitable months", f"{int((mok.pnl > 0).sum())} of {len(mok)}"
                                  + (" (includes partial months)" if (mok.completeness != 'complete').any() else "")),
            ("Time underwater", f"{pct(sm.get('pct_time_underwater'))} of {ctx.notice.get('n_obs', 0)} sessions"),
            ("Longest without a new high", f"{stg.get('longest_days', '—')} days"),
            ("Recovery factor", ratio((u['total'] / abs(sm['max_dd'])) if sm.get('max_dd') else None)),
        ]
        st.table(pd.DataFrame(rows, columns=["Metric", "Value"]).set_index("Metric"))
    with right:
        st.subheader("Returns & risk-adjusted", divider="gray")
        if ctx.capital_path is None:
            st.info(f"% metrics unavailable: {ctx.capital_reason}  \nRupee analytics above remain valid.")
        else:
            cp = ctx.capital_path
            if rets["short_history"]:
                st.warning(f"Short history: {rets['n_days']} daily returns over {rets['calendar_days']} calendar days. "
                           "Annualised figures (CAGR, Sharpe, Sortino, Calmar) extrapolate a short sample — treat as indicative only.")
            rows = [
                ("Starting capital (observed start)", lakh(cp["start_equity"].iloc[0])),
                ("Total return (time-weighted)", pct(rets["total_return"], 2)),
                ("CAGR (annualised TWR)", pct(rets["cagr"], 1)),
                ("Money-weighted return (XIRR)", pct(rets["mwr"], 1)),
                ("Annualised volatility", pct(rets["vol"], 1)),
                (f"Sharpe (rf {pct(CALC['risk_free_annual'])}, ×√{CALC['annualisation_days']})", ratio(rets["sharpe"])),
                (f"Sortino (target {pct(CALC['sortino_target_annual'])})", ratio(rets["sortino"])),
                ("Max drawdown % (TWR index)", pct(rets["max_dd_pct"], 2)),
                ("Calmar-style (CAGR ÷ |max DD %|)", ratio(rets["calmar"])),
            ]
            st.table(pd.DataFrame(rows, columns=["Metric", "Value"]).set_index("Metric"))
            st.caption("Daily returns = day's realised P&L ÷ (prior equity + same-day flows). Capital from config/capital.csv. "
                       "Same observed window as the rupee metrics.")

    st.subheader("Systems in selection", divider="gray")
    mat = P.system_daily_matrix(ctx.legs, ctx.obs.index)
    comp = P.comparison(ctx.legs, mat, SHORT, TOL, BE) if not ctx.legs.empty else pd.DataFrame()
    if not comp.empty:
        show = comp[["name", "net_pnl", "contribution", "legs", "win_rate", "profit_factor", "max_dd",
                     "longest_underwater_days", "current_dd", "current_underwater_days"]].sort_values("net_pnl", ascending=False)
        st.dataframe(show, hide_index=True, width="stretch", column_config={
            "name": "System", "net_pnl": st.column_config.NumberColumn(f"P&L ({BASIS})", format="₹ %.0f"),
            "contribution": st.column_config.NumberColumn("Share of total", format="percent"),
            "legs": "Legs", "win_rate": st.column_config.NumberColumn("Win rate", format="percent"),
            "profit_factor": st.column_config.NumberColumn("PF", format="%.2f"),
            "max_dd": st.column_config.NumberColumn("Max DD ₹", format="₹ %.0f"),
            "longest_underwater_days": st.column_config.NumberColumn("Longest underwater (d)"),
            "current_dd": st.column_config.NumberColumn("Current DD ₹", format="₹ %.0f"),
            "current_underwater_days": st.column_config.NumberColumn("Current underwater (d)")})
        st.caption("Leg-level stats per system. Share of total is undefined in sign when the total is negative — read the ₹ column.")

    head = R.headline_table(ctx)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        pd.DataFrame({"field": ["generated_at", "selection", "date_from", "date_to", "basis", "unit", "dd_baseline"],
                      "value": [pd.Timestamp.now(tz="Asia/Kolkata").isoformat(timespec="seconds"), sel_caption(),
                                SEL.start, SEL.end, BASIS, UNIT, DDMODE]}).to_excel(xw, sheet_name="selection", index=False)
        head.to_excel(xw, sheet_name="summary", index=False)
        ctx.daily.reset_index().to_excel(xw, sheet_name="daily", index=False)
        ctx.episodes.to_excel(xw, sheet_name="drawdown_episodes", index=False)
        if not comp.empty:
            comp.to_excel(xw, sheet_name="systems", index=False)
    c1, c2 = st.columns(2)
    c1.download_button("⬇ Summary export (Excel)", buf.getvalue(), "quantleap_summary.xlsx",
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    with c2:
        download(head, "summary_metrics", "⬇ Summary metrics (CSV)")

# ================================================================= TAB 2 EQUITY & DRAWDOWNS
with tabs[1]:
    pchart(st, equity_fig(show_accounts=st.toggle("Show per-account lines", False)))
    if ctx.capital_path is not None:
        cp = ctx.capital_path
        fig = go.Figure()
        fig.add_scatter(x=cp.index, y=cp["equity"], mode="lines", name="Capital-based equity", line=dict(color=LINE))
        pchart(st, plot_layout(fig, 260, "₹ capital + realised P&L"))
        st.caption("Capital-based equity = recorded capital flows + cumulative realised P&L (not mark-to-market).")

    c1, c2 = st.columns(2)
    if not ctx.underwater.empty:
        uw = ctx.underwater
        fig = go.Figure(go.Scatter(x=uw.index, y=uw["drawdown"], fill="tozeroy", mode="lines",
                                   line=dict(color=LOSS), name="₹ below peak"))
        pchart(c1, plot_layout(fig, 280, "₹ below running peak"))
        c1.caption("Monetary underwater curve (realised, daily close).")
    if ctx.capital_path is not None:
        idx = ctx.capital_path["twr_index"]
        ddp = idx / np.maximum.accumulate(np.concatenate([[1.0], idx.values]))[1:] - 1
        fig = go.Figure(go.Scatter(x=idx.index, y=ddp * 100, fill="tozeroy", mode="lines", line=dict(color=LOSS)))
        pchart(c2, plot_layout(fig, 280, "% below peak (TWR)"))
        c2.caption("Percentage underwater on the time-weighted index — deposits/withdrawals cannot create or hide drawdowns.")
    else:
        c2.info(f"% underwater unavailable: {ctx.capital_reason}")

    st.subheader("Drawdown headline", divider="gray")
    c = st.columns(4)
    metric(c[0], "Deepest drawdown", inr(sm.get("max_dd")),
           delta=(f"{fmt_date(deep['peak_date'])} → {fmt_date(deep['trough_date'])}" if deep else None))
    metric(c[1], "Longest time underwater", f"{longest['underwater_days']} d" if longest else "0 d",
           delta=(f"{fmt_date(longest['peak_date'])} → {fmt_date(longest['recovery_date']) if longest['recovery_date'] else 'ongoing'}" if longest else None))
    metric(c[2], "Recovery time of deepest",
           (f"{int(deep['trough_to_recovery_days'])} d" if deep and deep["status"] == "Recovered" else ("not recovered" if deep else "—")),
           "Trough-to-recovery calendar days of the deepest episode.")
    metric(c[3], "Current drawdown", inr(cur["current_depth"]) if cur else "At peak",
           delta=(f"{cur['underwater_days']} d elapsed" if cur else None))
    c = st.columns(4)
    metric(c[0], "Episodes", sm.get("n_episodes", 0))
    metric(c[1], "% sessions underwater", pct(sm.get("pct_time_underwater")))
    metric(c[2], "Longest without new high", f"{stg.get('longest_days', '—')} d",
           "Includes flat periods at the peak (recovered but no new high).")
    metric(c[3], "Since last new high", f"{stg.get('current_days', '—')} d",
           delta=(f"last high {fmt_date(stg.get('last_new_high'))}" if stg else None))
    st.caption(f"Recovered = back within ₹{TOL:g} of the prior peak. Baseline: "
               f"{'₹0 at range start' if DDMODE == 'reset' else 'carried from earlier observed history'}. "
               "Drawdowns are measured over observed history; a peak before the first available record cannot be seen "
               "(episodes starting at 'start of observed history' may have begun earlier — start unknown).")

    ep = ctx.episodes.copy()
    if not ep.empty:
        ep.insert(0, "#", range(1, len(ep) + 1))
        epc = {"depth": st.column_config.NumberColumn("Depth ₹", format="₹ %.0f"),
               "depth_pct": st.column_config.NumberColumn("Depth % (of equity at peak)", format="percent"),
               "peak_note": "Peak note", "peak_to_trough_days": "Peak→trough (d)",
               "trough_to_recovery_days": "Trough→recovery (d)", "underwater_days": "Total underwater (d)",
               "underwater_sessions": "Underwater sessions", "peak_to_trough_sessions": "Peak→trough (sessions)",
               "trough_to_recovery_sessions": "Trough→recovery (sessions)"}
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Deepest episodes**")
            st.dataframe(ep.sort_values("depth").head(5)[["#", "status", "peak_date", "trough_date", "recovery_date", "depth",
                                                          "underwater_days"]], hide_index=True, column_config=epc)
        with c2:
            st.markdown("**Longest episodes**")
            st.dataframe(ep.sort_values("underwater_days", ascending=False).head(5)[
                ["#", "status", "peak_date", "recovery_date", "underwater_days", "underwater_sessions", "depth"]],
                hide_index=True, column_config=epc)
        fig = px.scatter(ep, x="underwater_days", y="depth", color="status", size=ep["depth"].abs().clip(lower=1),
                         hover_data=["#", "peak_date", "trough_date", "recovery_date"],
                         color_discrete_map={"Recovered": LINE, "Ongoing": LOSS})
        fig.update_xaxes(title="Total time underwater (calendar days)")
        fig.update_yaxes(title="Depth ₹")
        pchart(st, plot_layout(fig, 320))
        st.caption("Depth vs duration: bottom-left = shallow & short; **far right near zero = prolonged shallow drawdowns** "
                   "that a ₹-only view would miss.")
        st.markdown("**All episodes**")
        st.dataframe(ep, hide_index=True, width="stretch", column_config=epc)
        download(ep, "drawdown_episodes")
    else:
        st.success("No drawdown episodes in the observed window.")

# ================================================================= TAB 3 SYSTEMS & BASKETS
with tabs[2]:
    mat = P.system_daily_matrix(ctx.legs, ctx.obs.index)
    if ctx.legs.empty:
        st.info("No data in selection.")
    else:
        view = st.radio("Compare", ["Systems", "Accounts", "Logical strategies (from aliases)"], horizontal=True)
        if view == "Systems":
            comp = P.comparison(ctx.legs, mat, SHORT, TOL, BE)
        elif view == "Accounts":
            comp = P.comparison(ctx.legs, mat, {a: a for a in ACCOUNTS}, TOL, BE, group_col="client_id")
        else:
            lg = ctx.legs.copy()
            lg["logical"] = lg["logical_strategy"].replace("", np.nan).fillna(lg["system_key"].map(SHORT))
            lmap = lg.groupby("system_key")["logical"].first()
            lmat = mat.T.groupby(lmap.reindex(mat.columns).values).sum().T
            comp = P.comparison(lg.assign(system_key=lg["logical"]), lmat, {}, TOL, BE)
            st.caption("Systems are grouped only where config/strategy_aliases.csv sets the same `logical_strategy`. "
                       "None set yet → each system is its own row.")
        cfg = {"name": "Name", "net_pnl": st.column_config.NumberColumn(f"P&L {BASIS}", format="₹ %.0f"),
               "contribution": st.column_config.NumberColumn("Share", format="percent"),
               "win_rate": st.column_config.NumberColumn("Win rate", format="percent"),
               "profit_factor": st.column_config.NumberColumn("PF", format="%.2f"),
               "payoff": st.column_config.NumberColumn("Payoff", format="%.2f"),
               "expectancy": st.column_config.NumberColumn("Expectancy", format="₹ %.0f"),
               "max_dd": st.column_config.NumberColumn("Max DD ₹", format="₹ %.0f"),
               "longest_underwater_days": "Longest UW (d)", "current_dd": st.column_config.NumberColumn("Current DD ₹", format="₹ %.0f"),
               "current_underwater_days": "Current UW (d)",
               "pct_time_underwater": st.column_config.NumberColumn("% time UW", format="percent"),
               "recovery_factor": st.column_config.NumberColumn("Recovery factor", format="%.2f")}
        st.dataframe(comp.drop(columns=["key"]), hide_index=True, width="stretch", column_config=cfg)
        st.caption("Click a column header to sort. Money and time-based drawdown shown together — don't rank on P&L or win rate alone. "
                   "Each system's DD is computed on its own daily realised path over the same observed days.")
        download(comp, f"comparison_{view.split()[0].lower()}")

        if view == "Systems":
            fig = px.bar(comp.sort_values("net_pnl"), x="net_pnl", y="name", orientation="h",
                         color=comp.sort_values("net_pnl")["net_pnl"] > 0, color_discrete_map={True: GAIN, False: LOSS})
            fig.update_layout(showlegend=False, yaxis_title=None, xaxis_title=f"₹ contribution ({BASIS})")
            pchart(st, plot_layout(fig, 30 + 22 * len(comp)))

        st.subheader("Correlation of daily P&L", divider="gray")
        c1, c2 = st.columns([1, 3])
        active_only = c1.toggle("Only days both traded", False,
                                help="Off: all observed days (no-trade = ₹0). On: only days both systems closed something.")
        min_obs = c1.number_input("Min overlapping days", 5, 200, 20)
        corr, nobs = P.correlations(mat, int(min_obs), active_only)
        corr.index = corr.columns = [SHORT.get(k, k) for k in corr.columns]
        nobs.index = nobs.columns = corr.columns
        fig = px.imshow(corr, zmin=-1, zmax=1, color_continuous_scale="RdBu_r", aspect="auto", text_auto=".2f")
        fig.update_traces(customdata=nobs.values, hovertemplate="%{y} × %{x}<br>ρ=%{z:.2f}<br>n=%{customdata} days")
        pchart(c2, plot_layout(fig, 120 + 28 * len(corr)))
        st.caption(f"Pearson on realised daily P&L; blank = fewer than {int(min_obs)} overlapping days or no variance. "
                   "Hover for the observation count. Correlation of realised P&L understates intraday co-movement.")

        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Simultaneous losses (pairs)**")
            jl = P.joint_losses(mat)
            if not jl.empty:
                jl["a"], jl["b"] = jl["a"].map(SHORT), jl["b"].map(SHORT)
                st.dataframe(jl.head(15), hide_index=True, column_config=money_cols(jl, ["joint_loss_inr"]))
                download(jl, "joint_losses")
        with c2:
            st.markdown("**Breadth: systems losing on the same day**")
            br = P.breadth_of_losses(mat)
            if not br.empty:
                agg = br.groupby("systems_losing").agg(days=("basket_pnl", "size"), avg_basket=("basket_pnl", "mean"),
                                                       total_basket=("basket_pnl", "sum")).reset_index()
                st.dataframe(agg, hide_index=True, column_config=money_cols(agg, ["avg_basket", "total_basket"]))

        st.subheader("Who drove the worst drawdowns", divider="gray")
        wep, wc = P.worst_dd_contributions(mat, TOL)
        if not wc.empty:
            wc.index = [SHORT.get(k, k) for k in wc.index]
            wc = wc.sort_values(wc.columns[0])
            st.dataframe(wc, width="stretch",
                         column_config={c: st.column_config.NumberColumn(c, format="₹ %.0f") for c in wc.columns})
            st.caption("Each column = one of the basket's deepest episodes; values = each system's realised P&L from peak to trough.")

        st.subheader("Leave-one-system-out (hypothetical)", divider="gray")
        loo = P.leave_one_out(mat, SHORT, TOL)
        st.dataframe(loo, hide_index=True, width="stretch", column_config={
            **money_cols(loo, ["net_pnl", "max_dd", "current_dd", "Δ net vs full", "Δ max DD vs full"]),
            "pct_underwater": st.column_config.NumberColumn("% time UW", format="percent"),
            "recovery_factor": st.column_config.NumberColumn("Recovery factor", format="%.2f"),
            "longest_uw_days": "Longest UW (d)", "current_uw_days": "Current UW (d)"})
        st.caption("Removes one system and recomputes on the same observed days. Hypothetical — ignores any capital or "
                   "margin interaction. Positive Δ max DD = shallower without that system.")

        sz = P.sizing_changes(ctx.legs, SHORT)
        with st.expander(f"Sizing changes detected ({len(sz)})"):
            st.dataframe(sz, hide_index=True, width="stretch")
            st.caption("Quantity changes between consecutive legs. Rupee comparisons across systems/periods mix sizes; "
                       "normalise mentally or via capital allocation before ranking.")

    st.subheader("Manage named baskets", divider="gray")
    with st.expander("Create / edit / delete a basket"):
        names = ["(new basket)"] + sorted(BASKETS)
        pick = st.selectbox("Basket", names)
        nm = st.text_input("Name", "" if pick == "(new basket)" else pick)
        cur_members = sorted(BASKETS.get(pick, set()))
        mem = st.multiselect("Members (account-specific systems)", list(SYS.system_key), default=cur_members,
                             format_func=lambda k: LABEL.get(k, k))
        c1, c2 = st.columns(2)
        if c1.button("Save basket", type="primary"):
            con = connect(S.database)
            try:
                if pick != "(new basket)" and nm != pick:
                    delete_basket(con, pick)
                save_basket(con, nm, mem)
                st.success(f"Saved '{nm}' with {len(mem)} systems.")
            finally:
                con.close()
            fresh(); st.rerun()
        if pick != "(new basket)" and c2.button("Delete basket"):
            con = connect(S.database)
            delete_basket(con, pick); con.close()
            fresh(); st.rerun()
        st.caption("'QL · …' baskets are seeded from QuantLead's basket names and auto-extend with new strategies "
                   "until you edit them.")

# ================================================================= TAB 4 CALENDAR & CONSISTENCY
with tabs[3]:
    if ctx.obs.empty:
        st.info("No observed days.")
    else:
        freq = st.radio("Period", ["Daily", "Weekly", "Monthly", "Yearly"], horizontal=True, index=2)
        if freq == "Daily":
            pt = ctx.daily.copy()
            pt.index = pt.index.map(str)
            pt["completeness"] = pt["status"]
        else:
            pt = M.period_table(ctx.obs, freq[0], ctx.daily)
        valid = pt[pt["completeness"] != "missing"]
        c = st.columns(4)
        metric(c[0], f"Profitable {freq.lower()} periods", f"{int((valid.pnl > 0).sum())} / {len(valid)}",
               "Over periods with any observed data. Partial periods included and flagged in the table.")
        metric(c[1], "Best period", inr(valid.pnl.max()), delta=str(valid.pnl.idxmax()) if len(valid) else None)
        metric(c[2], "Worst period", inr(valid.pnl.min()), delta=str(valid.pnl.idxmin()) if len(valid) else None)
        metric(c[3], "Partial / missing periods",
               f"{int(pt.completeness.str.startswith('partial').sum())} / {int((pt.completeness == 'missing').sum())}")
        colors = np.where(pt["pnl"] >= 0, GAIN, LOSS)
        fig = go.Figure(go.Bar(x=pt.index, y=pt["pnl"], marker_color=colors,
                               marker_pattern_shape=np.where(pt["completeness"] == "complete", "", "/"),
                               customdata=pt["completeness"], hovertemplate="%{x}<br>₹%{y:,.0f}<br>%{customdata}"))
        pchart(st, plot_layout(fig, 300, f"₹ {BASIS}"))
        st.caption("Hatched bars = partial periods. Missing periods are blank (unknown), not zero. ₹ P&L, not % returns.")
        st.dataframe(pt, width="stretch", column_config=money_cols(pt, ["pnl"]))
        download(pt.reset_index(), f"periods_{freq.lower()}")

        st.subheader("Monthly heatmap by system", divider="gray")
        if not ctx.legs.empty:
            hm = ctx.legs.assign(month=pd.to_datetime(ctx.legs.close_date).dt.to_period("M").astype(str))
            hm = hm.pivot_table(index="system_key", columns="month", values="pnl_basis", aggfunc="sum")
            hm.index = [SHORT.get(k, k) for k in hm.index]
            mstat = M.period_table(ctx.obs, "M", ctx.daily)["completeness"]
            hm.columns = [f"{m}{'*' if mstat.get(m, 'complete') != 'complete' else ''}" for m in hm.columns]
            lim = float(np.nanmax(np.abs(hm.values))) if hm.size else 1
            fig = px.imshow(hm, color_continuous_scale="RdYlGn", zmin=-lim, zmax=lim, aspect="auto", text_auto=",.0f")
            pchart(st, plot_layout(fig, 120 + 26 * len(hm)))
            st.caption("* = partial month (range edge, missing or incomplete days). Blank cell = system closed nothing that month.")

        st.subheader("Daily calendar", divider="gray")
        dd_ = ctx.daily.copy()
        dd_.index = pd.to_datetime(dd_.index)
        dd_["week"] = dd_.index.to_period("W-SUN").start_time.date
        dd_["weekday"] = dd_.index.day_name().str[:3]
        cal = dd_.pivot_table(index="weekday", columns="week", values="pnl", aggfunc="sum")
        cal = cal.reindex(["Mon", "Tue", "Wed", "Thu", "Fri"])
        lim = float(np.nanmax(np.abs(cal.values))) if cal.size else 1
        fig = px.imshow(cal, color_continuous_scale="RdYlGn", zmin=-lim, zmax=lim, aspect="auto")
        pchart(st, plot_layout(fig, 260))
        st.caption("Columns = week starting Monday. Blank = holiday, missing or outside range.")

        st.subheader("Rolling metrics", divider="gray")
        win = st.slider("Window (observed sessions)", 5, 60, 20)
        ro = ctx.obs["pnl"]
        roll = pd.DataFrame({"Rolling P&L": ro.rolling(win).sum(),
                             "Rolling % profitable days": (ro > 0).astype(float).where(ctx.obs.n_closes > 0).rolling(win, min_periods=max(3, win // 2)).mean() * 100})
        fig = go.Figure()
        fig.add_scatter(x=roll.index, y=roll["Rolling P&L"], name=f"{win}-session P&L (₹)", line=dict(color=LINE))
        fig.add_scatter(x=roll.index, y=roll["Rolling % profitable days"], name="% profitable days", yaxis="y2",
                        line=dict(color=MUTED, dash="dot"))
        fig.update_layout(yaxis2=dict(overlaying="y", side="right", range=[0, 100], title="%"))
        pchart(st, plot_layout(fig, 300, "₹"))

        st.subheader("Breakdowns", divider="gray")
        L = ctx.legs.copy()
        L["weekday_close"] = pd.to_datetime(L.close_date).dt.day_name()
        L["month_close"] = pd.to_datetime(L.close_date).dt.to_period("M").astype(str)
        L["entry_weekday"] = pd.to_datetime(L.entry_date).dt.day_name()
        dim = st.selectbox("Break down by", ["instrument", "option_type", "action", "exit_type", "entry_weekday",
                                             "weekday_close", "month_close", "exchange", "basket_name"])
        bd = L.groupby(dim).agg(legs=("pnl_basis", "size"), pnl=("pnl_basis", "sum"), avg=("pnl_basis", "mean"),
                                win_rate=("pnl_basis", lambda x: (x > BE).mean())).reset_index()
        st.dataframe(bd, hide_index=True, column_config={**money_cols(bd, ["pnl", "avg"]),
                                                         "win_rate": st.column_config.NumberColumn("Win rate", format="percent")})

        st.subheader("Concentration — dependence on best outcomes", divider="gray")
        c1, c2 = st.columns(2)
        unit_vals = ctx.units["pnl_basis"]
        ct = M.concentration(unit_vals)
        cd = M.concentration(ctx.obs.loc[ctx.obs.n_closes > 0, "pnl"])
        cfgc = {**money_cols(ct, ["top_sum", "total_excluding"]),
                "share_of_total": st.column_config.NumberColumn("Share of total P&L", format="percent")}
        c1.markdown(f"**Top {UNIT}s**"); c1.dataframe(ct, hide_index=True, column_config=cfgc)
        c2.markdown("**Top days**"); c2.dataframe(cd, hide_index=True, column_config=cfgc)
        st.caption("'Total excluding' is a hypothetical sensitivity: total P&L if the best N had not happened. "
                   "Share > 100% means the rest of the sample is net negative.")

# ================================================================= TAB 5 TRADES & DURATION
with tabs[4]:
    st.markdown(f"**Reporting unit:** {'derived strategy runs (legs grouped by strategy + execution date)' if UNIT == 'run' else 'legs — one row = one filled option position from entry fill to full exit'}. "
                "Exit timestamps are **not** in the source, so holding time is measured in sessions/calendar days "
                "from entry fill date to close date.")
    q = st.text_input("Search (symbol, system, exit type, position id…)", "")
    if UNIT == "run":
        tbl = ctx.units.copy()
    else:
        tbl = ctx.legs[["close_date", "entry_ts", "account_label", "display_name", "basket_name", "symbol", "instrument",
                        "option_type", "strike", "action", "quantity", "expected_entry_price", "average_entry_price",
                        "exit_average_price", "pnl", "est_charges", "net_est_pnl", "exit_type", "sl_status",
                        "hold_sessions", "hold_cal_days", "entry_slippage_inr", "position_id", "run_key",
                        "provenance"]].copy()
        tbl["entry_ts"] = tbl["entry_ts"].dt.strftime("%Y-%m-%d %H:%M:%S")
    if q:
        m = tbl.astype(str).apply(lambda col: col.str.contains(q, case=False, regex=False)).any(axis=1)
        tbl = tbl[m]
    st.dataframe(tbl, hide_index=True, width="stretch", height=380,
                 column_config=money_cols(tbl, ["pnl", "pnl_basis", "est_charges", "net_est_pnl", "entry_slippage_inr"]))
    st.caption(f"{len(tbl)} rows. Provenance = source file : CSV line where the record was first seen.")
    download(tbl, f"trades_{UNIT}s")

    if not ctx.legs.empty:
        L = ctx.legs
        st.subheader("Holding duration", divider="gray")
        c = st.columns(4)
        metric(c[0], "Mean holding (sessions)", num(L.hold_sessions.mean(), 2))
        metric(c[1], "Median holding (sessions)", num(L.hold_sessions.median(), 1))
        w, l_ = L[L.pnl_basis > BE], L[L.pnl_basis < -BE]
        metric(c[2], "Winners — mean sessions", num(w.hold_sessions.mean(), 2))
        metric(c[3], "Losers — mean sessions", num(l_.hold_sessions.mean(), 2))
        hb = L.groupby("hold_band", observed=False).agg(legs=("pnl_basis", "size"), pnl=("pnl_basis", "sum"),
                                                        avg=("pnl_basis", "mean"),
                                                        win_rate=("pnl_basis", lambda x: (x > BE).mean() if len(x) else np.nan)).reset_index()
        c1, c2 = st.columns(2)
        c1.dataframe(hb, hide_index=True, column_config={**money_cols(hb, ["pnl", "avg"]),
                                                         "win_rate": st.column_config.NumberColumn("Win rate", format="percent")})
        fig = px.histogram(L, x="hold_sessions", color=np.where(L.pnl_basis > BE, "Winner", np.where(L.pnl_basis < -BE, "Loser", "Breakeven")),
                           barmode="group", color_discrete_map={"Winner": GAIN, "Loser": LOSS, "Breakeven": MUTED})
        fig.update_xaxes(title="Sessions held (entry date → close date)", dtick=1)
        pchart(c2, plot_layout(fig, 260))
        st.caption("Holding duration ≠ drawdown duration. 0 = closed the same session as entry.")

        st.subheader("Entry time", divider="gray")
        L2 = L.assign(entry_slot=L.entry_ts.dt.floor("30min").dt.strftime("%H:%M"))
        et = L2.groupby("entry_slot").agg(legs=("pnl_basis", "size"), pnl=("pnl_basis", "sum"),
                                          avg=("pnl_basis", "mean"), win_rate=("pnl_basis", lambda x: (x > BE).mean())).reset_index()
        fig = px.bar(et, x="entry_slot", y="pnl", color=et.pnl > 0, color_discrete_map={True: GAIN, False: LOSS},
                     hover_data=["legs", "avg", "win_rate"])
        fig.update_layout(showlegend=False, xaxis_title="Entry fill time (30-min slot, IST)")
        pchart(st, plot_layout(fig, 260, f"₹ {BASIS}"))
        st.caption("Exit-time analysis is unavailable: the export has no exit timestamp.")

        st.subheader("Execution: entry slippage vs expected price", divider="gray")
        c = st.columns(3)
        metric(c[0], "Total entry slippage", inr(L.entry_slippage_inr.sum()),
               "(expected − fill) × qty × side. Positive = filled better than the strategy's expected entry price.")
        metric(c[1], "Median per leg", inr(L.entry_slippage_inr.median()))
        metric(c[2], "Legs filled worse than expected", f"{int((L.entry_slippage_inr < 0).sum())} / {len(L)}")
        st.caption("Entry side only; there is no expected exit price in the source, so exit slippage cannot be measured.")

# ================================================================= TAB 6 DATA & METHODOLOGY
with tabs[5]:
    last_ok = RUNS[RUNS.status == "committed"].finished_at.max() if not RUNS.empty else None
    c = st.columns(4)
    metric(c[0], "Confirmed legs in ledger", f"{int(legs_all.filled.sum()):,}")
    metric(c[1], "All records (incl. cancelled)", f"{len(legs_all):,}")
    metric(c[2], "Open review items", int((REVIEW.status == "open").sum()) if not REVIEW.empty else 0)
    metric(c[3], "Last successful import", (last_ok or "—")[:16].replace("T", " "))

    st.subheader("Coverage & retention", divider="gray")
    st.markdown(f"QuantLead keeps closed positions for **~{S.retention_days} days**; this app keeps every imported record "
                "permanently. Each export proves complete detail from its earliest close date to its as-of time.")
    st.dataframe(IV.rename(columns={"original_name": "evidence file"}), hide_index=True, width="stretch")
    ds_all = C.day_status(IV, ACCOUNTS, first_cov - timedelta(days=7), last_cov, HOL)
    if not ds_all.empty:
        code = ds_all.replace({"covered": 2, "partial": 1, "missing": 0}).astype(int).T
        fig = px.imshow(code, color_continuous_scale=[[0, "#d9d9d9"], [0.5, "#fdae61"], [1, "#1a9850"]], zmin=0, zmax=2,
                        aspect="auto")
        fig.update_coloraxes(showscale=False)
        pchart(st, plot_layout(fig, 160))
        st.caption("Green = covered (no-trade days are verified zero) · orange = partial day · grey = missing (unknown). "
                   "Holidays (config/holidays.csv) and weekends are excluded.")

    st.subheader("Imported files", divider="gray")
    st.dataframe(FILES[["file_id", "original_name", "status", "schema_name", "row_count", "file_mtime_ist", "first_seen_at",
                        "archived_path", "sha256"]], hide_index=True, width="stretch")
    st.subheader("Import history", divider="gray")
    st.dataframe(RUNS.drop(columns=["report_json"]), hide_index=True, width="stretch")
    st.caption(f"Revisions recorded: " + (", ".join(f"{r.change_type}: {r.n}" for r in REVS.itertuples()) or "none"))

    st.subheader("Review queue (conflicts, ambiguous identities, invalid rows)", divider="gray")
    openq = REVIEW[REVIEW.status == "open"] if not REVIEW.empty else REVIEW
    if openq.empty:
        st.success("Nothing awaiting review. Unresolved items would be excluded from confirmed performance.")
    else:
        st.warning(f"{len(openq)} open item(s) excluded from confirmed results. Potential P&L impact where calculable: "
                   f"{inr(openq.pnl_impact.fillna(0).sum())}.")
        st.dataframe(openq, hide_index=True, width="stretch")
        rid = st.selectbox("Item", list(openq.review_id))
        note = st.text_input("Resolution note")
        c1, c2 = st.columns(2)
        if c1.button("Accept incoming version"):
            st.write(resolve_review(S, int(rid), True, note)); fresh(); st.rerun()
        if c2.button("Reject (keep existing)"):
            st.write(resolve_review(S, int(rid), False, note)); fresh(); st.rerun()

    st.subheader("Reconciliation", divider="gray")
    src = SIGHT[SIGHT.entry_status == "COMPLETE"].groupby(["file_id", "client_id"]).agg(
        legs_in_file=("pnl", "size"), pnl_in_file=("pnl", "sum")).reset_index()
    led = legs_all[legs_all.filled].groupby("client_id").agg(ledger_legs=("pnl", "size"), ledger_pnl=("pnl", "sum")).reset_index()
    rec = src.merge(led, on="client_id", how="outer")
    rec["Δ legs"] = rec["ledger_legs"] - rec["legs_in_file"]
    rec["Δ P&L"] = rec["ledger_pnl"] - rec["pnl_in_file"]
    st.dataframe(rec, hide_index=True, width="stretch",
                 column_config=money_cols(rec, ["pnl_in_file", "ledger_pnl", "Δ P&L"]))
    st.caption("Per source file vs the ledger for that account. Non-zero Δ is expected once older history is retained "
               "after QuantLead purges it, or when several exports overlap — the ledger is the union of all of them.")
    tot_sel = float(ctx.legs.pnl.sum()) if not ctx.legs.empty else 0.0
    by_acc = ctx.legs.groupby("client_id").pnl.sum()
    by_sys = ctx.legs.groupby("system_key").pnl.sum()
    st.markdown(f"Selection check (gross): total {inr(tot_sel)} = Σ accounts {inr(by_acc.sum())} = Σ systems {inr(by_sys.sum())} "
                f"→ {'✅ consistent' if abs(tot_sel - by_acc.sum()) < 0.01 and abs(tot_sel - by_sys.sum()) < 0.01 else '❌ mismatch'}")

    st.subheader("Data-quality checks", divider="gray")
    F = legs_all
    dq = pd.DataFrame([
        ("Cancelled / rejected entries (kept, excluded from performance)", int((~F.filled).sum())),
        ("Filled legs with P&L exactly 0 (breakeven)", int((F.filled & (F.pnl == 0)).sum())),
        ("Filled legs where stated P&L ≠ price arithmetic (> ₹1)",
         int((F.filled & ((F.exit_average_price - F.average_entry_price) * F.quantity * np.where(F.action == 'BUY', 1, -1) - F.pnl).abs().gt(1)).sum())),
        ("Filled legs entered on a later day than execution_date", int((F.filled & (F.entry_date != F.exec_date)).sum())),
        ("Closed with blank exit_status", int((F.filled & (F.exit_status == "")).sum())),
        ("Closes on weekend/holiday dates", int(F.close_date.map(lambda d: d.weekday() >= 5 or d in HOL).sum())),
        ("Fallback (fingerprint) identities", int((F.identity_basis != "source_id").sum())),
        ("Strategy names used by >1 strategy_id", int((F.groupby("strategy_name").strategy_id.nunique() > 1).sum())),
    ], columns=["Check", "Count"])
    st.table(dq.set_index("Check"))

    st.subheader("Source-reported aggregates (kept separate)", divider="gray")
    if ROLLUPS.empty:
        st.info("None imported. If you export QuantLead aggregates (Top Trades, Distribution, breakdowns), save them in the "
                "rollup CSV format (see docs/README.md) — they will be shown here only, never mixed into detailed analytics.")
    else:
        st.dataframe(ROLLUPS, hide_index=True, width="stretch")
        st.caption("Cumulative snapshots are never summed; aggregates are not converted to trades or curves.")

    st.subheader("Mappings", divider="gray")
    st.dataframe(load_aliases(S), hide_index=True, width="stretch")
    st.caption("Edit config/strategy_aliases.csv to rename systems or set a shared `logical_strategy` across accounts "
               "(only with evidence that they are the same logic). New strategies are appended automatically on import.")
    bk = pd.DataFrame([(b, SHORT.get(k, k)) for b, ks in BASKETS.items() for k in sorted(ks)], columns=["basket", "system"])
    with st.expander(f"Basket membership ({len(BASKETS)} baskets)"):
        st.dataframe(bk, hide_index=True, width="stretch")

    st.subheader("Capital & cash flows", divider="gray")
    st.caption("One row per dated event. Types: OPENING, DEPOSIT, WITHDRAWAL, TRANSFER_IN, TRANSFER_OUT (enter amounts "
               "positive). Transfers between two included accounts net out at basket level. Flows apply at the start of their date.")
    cap_edit = st.data_editor(CAPITAL[CAPITAL_COLUMNS] if not CAPITAL.empty else pd.DataFrame(columns=CAPITAL_COLUMNS),
                              num_rows="dynamic", width="stretch", key="cap_editor",
                              column_config={"date": st.column_config.DateColumn("date"),
                                             "type": st.column_config.SelectboxColumn("type", options=["OPENING", "DEPOSIT", "WITHDRAWAL", "TRANSFER_IN", "TRANSFER_OUT"]),
                                             "client_id": st.column_config.SelectboxColumn("client_id", options=ACCOUNTS),
                                             "amount_inr": st.column_config.NumberColumn("amount_inr", format="₹ %.0f", min_value=0)})
    if st.button("Save capital changes"):
        try:
            save_capital(S, cap_edit)
            st.success("Saved config/capital.csv"); fresh(); st.rerun()
        except Exception as e:
            st.error(f"Not saved: {e}")

    st.subheader("Calculation settings in use", divider="gray")
    st.json({"calc": CALC, "charges_estimate": S.charges, "retention_days": S.retention_days})

    st.subheader("Unavailable metrics and why", divider="gray")
    st.table(pd.DataFrame([
        ("Mark-to-market equity, intraday/intratrade drawdown", "Source has closed positions only, no MTM or ticks."),
        ("Exit-time analysis, intraday holding minutes", "No exit timestamp in the export."),
        ("Actual net P&L / cost erosion", "No charges in the export; 'Estimated net' is a model (see settings)."),
        ("Exit slippage", "No expected exit price in the export."),
        ("R-multiples", "No planned initial risk per trade."),
        ("% metrics for strategy subsets", "Capital is recorded per account, not per strategy."),
        ("Anything before the earliest covered day", "Purged at source; backfill older exports if you have them."),
    ], columns=["Metric", "Reason"]).set_index("Metric"))

    st.subheader("Definitions & methodology", divider="gray")
    try:
        st.markdown((S.root / "docs" / "METHODOLOGY.md").read_text(encoding="utf-8"))
    except FileNotFoundError:
        st.info("docs/METHODOLOGY.md not found.")
