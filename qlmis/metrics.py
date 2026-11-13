"""Trade statistics, daily series, capital-based returns and risk-adjusted ratios."""
from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pandas as pd

from . import drawdown as D


# ---------------------------------------------------------------- trade / outcome statistics
def _streaks(signs: list[int]) -> tuple[int, int, int]:
    """(max win streak, max loss streak, current streak signed). Breakeven (0) resets streaks."""
    best_w = best_l = cur = 0
    for s in signs:
        if s > 0:
            cur = cur + 1 if cur > 0 else 1
        elif s < 0:
            cur = cur - 1 if cur < 0 else -1
        else:
            cur = 0
        best_w, best_l = max(best_w, cur), max(best_l, -cur)
    return best_w, best_l, cur


def outcome_stats(pnl: pd.Series, be_tol: float = 0.0) -> dict:
    """Stats over a sequence of outcomes (trades, runs or days), in chronological order.

    Denominators: win_rate = wins / all outcomes (breakevens included in the denominator);
    win_rate_decided = wins / (wins + losses). Undefined ratios are None, never 0.
    """
    x = pd.Series(pnl, dtype=float).dropna()
    n = len(x)
    win, loss = x[x > be_tol], x[x < -be_tol]
    be = n - len(win) - len(loss)
    gw, gl = float(win.sum()), float(loss.sum())
    aw = float(win.mean()) if len(win) else None
    al = float(loss.mean()) if len(loss) else None
    signs = [1 if v > be_tol else (-1 if v < -be_tol else 0) for v in x]
    sw, sl, cur = _streaks(signs)
    pw, pl = (len(win) / n, len(loss) / n) if n else (None, None)
    return dict(
        n=n, wins=len(win), losses=len(loss), breakevens=be,
        win_rate=pw, loss_rate=pl,
        win_rate_decided=(len(win) / (len(win) + len(loss))) if (len(win) + len(loss)) else None,
        total=float(x.sum()), gross_win=gw, gross_loss=gl,
        avg_win=aw, avg_loss=al,
        median_win=float(win.median()) if len(win) else None,
        median_loss=float(loss.median()) if len(loss) else None,
        payoff=(aw / abs(al)) if (aw is not None and al) else None,
        profit_factor=(gw / abs(gl)) if gl else (math.inf if gw > 0 else None),
        expectancy=(pw * (aw or 0) - pl * abs(al or 0)) if n else None,
        best=float(x.max()) if n else None, worst=float(x.min()) if n else None,
        max_win_streak=sw, max_loss_streak=sl, current_streak=cur,
    )


# ---------------------------------------------------------------- daily series
def daily_series(legs: pd.DataFrame, status: pd.Series, value_col: str = "pnl_basis") -> pd.DataFrame:
    """Daily realised P&L by close date over trading days of `status`.

    covered/partial days with no closes = 0 (verified no-trade); missing days = NaN (unknown).
    Returns frame: pnl, status, n_closes.
    """
    by_day = legs.groupby("close_date")[value_col].sum() if not legs.empty else pd.Series(dtype=float)
    cnt = legs.groupby("close_date").size() if not legs.empty else pd.Series(dtype=int)
    df = pd.DataFrame(index=status.index)
    df["status"] = status
    df["pnl"] = by_day.reindex(df.index).fillna(0.0)
    df["n_closes"] = cnt.reindex(df.index).fillna(0).astype(int)
    orphan = by_day.index.difference(df.index)
    df.loc[df["status"] == "missing", "pnl"] = np.nan
    df.attrs["orphan_close_days"] = list(orphan)  # closes on non-trading/holiday dates (data check)
    return df


def observed(daily: pd.DataFrame) -> pd.DataFrame:
    """Observed days only (covered + partial), from first to last observation."""
    return daily[daily["status"] != "missing"]


def dd_input_from_daily(obs: pd.DataFrame, col: str = "pnl", prior: pd.DataFrame | None = None,
                        mode: str = "reset") -> D.DDInput:
    """Cumulative path for drawdowns.

    mode='reset': baseline 0 at window start.
    mode='carry': path continues the full-history cumulative level; prior peak carried forward.
    """
    dates = list(obs.index)
    cum = obs[col].cumsum().to_numpy()
    first = dates[0] if dates else None
    base_date = (first - timedelta(days=1)) if first else None
    if mode == "carry" and prior is not None and not prior.empty:
        pcum = prior[col].cumsum()
        offset = float(pcum.iloc[-1])
        pk = float(max(pcum.max(), 0.0))
        pk_date = pcum.idxmax() if pcum.max() > 0 else (prior.index[0] - timedelta(days=1))
        return D.DDInput(dates, cum + offset, offset, base_date, "window start",
                         prior_peak=pk, prior_peak_date=pk_date)
    return D.DDInput(dates, cum, 0.0, base_date)


def period_table(obs_daily: pd.DataFrame, freq: str, status_daily: pd.DataFrame) -> pd.DataFrame:
    """Weekly/monthly/yearly P&L with completeness flags. freq in {'W','M','Y'}."""
    if status_daily.empty:
        return pd.DataFrame()
    idx = pd.to_datetime(status_daily.index)
    key = {"W": idx.to_period("W-SUN"), "M": idx.to_period("M"), "Y": idx.to_period("Y")}[freq]
    df = status_daily.assign(period=key)
    g = df.groupby("period")
    out = pd.DataFrame({
        "pnl": g["pnl"].sum(min_count=1),
        "trading_days": g.size(),
        "covered_days": g["status"].apply(lambda s: int((s == "covered").sum())),
        "partial_days": g["status"].apply(lambda s: int((s == "partial").sum())),
        "missing_days": g["status"].apply(lambda s: int((s == "missing").sum())),
        "closes": g["n_closes"].sum(),
    })
    # Calendar edges: a period whose trading days extend beyond the analysed range is incomplete.
    first, last = status_daily.index.min(), status_daily.index.max()
    starts = out.index.start_time.date
    ends = out.index.end_time.date
    out["window_cut"] = (starts < first) | (ends > last)
    out["completeness"] = np.select(
        [out["missing_days"] == out["trading_days"], out["missing_days"] > 0,
         out["partial_days"] > 0, out["window_cut"]],
        ["missing", "partial (missing days)", "partial (incomplete day)", "partial (range edge)"],
        "complete")
    out.loc[out["completeness"] == "missing", "pnl"] = np.nan
    out.index = out.index.astype(str)
    return out


# ---------------------------------------------------------------- capital-based returns
def capital_path(obs: pd.DataFrame, flows: pd.DataFrame, accounts: list[str]) -> pd.DataFrame | None:
    """Equity = capital flows + cumulative realised P&L, for whole-account selections.

    Flows dated on/before an observation day are applied at the START of that day.
    Daily return r_t = pnl_t / (equity_{t-1} + flow_t)   (time-weighted, flows excluded from return).
    Transfers between two included accounts net to zero automatically.
    """
    if obs.empty:
        return None
    f = flows[flows["client_id"].isin(accounts)]
    if f.empty:
        return None
    first = obs.index[0]
    f_before = float(f.loc[f["date"] < first, "signed_amount"].sum())
    fl = f[f["date"] >= first].groupby("date")["signed_amount"].sum()
    # Attach each flow to the first observation on/after its date.
    flow_on = pd.Series(0.0, index=obs.index)
    for d, amt in fl.items():
        nxt = obs.index[obs.index >= d]
        if len(nxt):
            flow_on[nxt[0]] += amt
    flow_on.iloc[0] += f_before
    eq_prev, rows = 0.0, []
    for d in obs.index:
        base = eq_prev + flow_on[d]
        p = obs.at[d, "pnl"]
        r = p / base if base > 0 else np.nan
        eq_prev = base + p
        rows.append((d, flow_on[d], base, p, r, eq_prev))
    out = pd.DataFrame(rows, columns=["date", "flow", "start_equity", "pnl", "ret", "equity"]).set_index("date")
    if out["start_equity"].iloc[0] <= 0:
        return None
    out["twr_index"] = (1 + out["ret"].fillna(0)).cumprod()
    return out


def xirr(cashflows: list[tuple[date, float]]) -> float | None:
    """Money-weighted return. Investor sign: deposits negative, final value positive."""
    if len(cashflows) < 2 or not any(c < 0 for _, c in cashflows) or not any(c > 0 for _, c in cashflows):
        return None
    t0 = cashflows[0][0]
    def npv(r):
        return sum(c / (1 + r) ** ((d - t0).days / 365.25) for d, c in cashflows)
    lo, hi = -0.9999, 100.0
    if npv(lo) * npv(hi) > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        if npv(lo) * npv(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def return_metrics(cp: pd.DataFrame | None, cfg: dict, dd_tol_frac: float = 1e-9) -> dict:
    """Return/risk metrics from the capital path. Everything None if capital is unavailable."""
    keys = ["total_return", "cagr", "vol", "sharpe", "sortino", "max_dd_pct", "calmar", "mwr",
            "n_days", "calendar_days", "short_history"]
    if cp is None or cp.empty:
        return dict.fromkeys(keys)
    A = float(cfg["annualisation_days"])
    r = cp["ret"].dropna()
    n = len(r)
    idx = cp["twr_index"]
    total = float(idx.iloc[-1] - 1)
    cal_days = (cp.index[-1] - cp.index[0]).days + 1
    cagr = (idx.iloc[-1]) ** (365.25 / cal_days) - 1 if cal_days > 0 and idx.iloc[-1] > 0 else None
    rf_d = (1 + cfg["risk_free_annual"]) ** (1 / A) - 1
    tgt_d = (1 + cfg["sortino_target_annual"]) ** (1 / A) - 1
    sd = float(r.std(ddof=1)) if n > 1 else None
    sharpe = ((r.mean() - rf_d) / sd * math.sqrt(A)) if sd else None
    downside = np.sqrt(np.mean(np.minimum(r - tgt_d, 0) ** 2)) if n else None
    sortino = ((r.mean() - tgt_d) / downside * math.sqrt(A)) if downside else None
    # % drawdown on the time-weighted index (deposits cannot mask or create drawdowns)
    inp = D.DDInput(list(cp.index), idx.to_numpy(), 1.0, cp.index[0] - timedelta(days=1))
    uw = D.underwater_series(inp, dd_tol_frac)
    max_dd_pct = float((uw["value"] / uw["peak"] - 1).min()) if len(uw) else None
    max_dd_pct = min(max_dd_pct, 0.0) if max_dd_pct is not None else None
    calmar = (cagr / abs(max_dd_pct)) if (cagr is not None and max_dd_pct) else None
    # Money-weighted (investor) return: flows in, ending equity out.
    cf = [(d, -float(v)) for d, v in cp["flow"].items() if v != 0]
    cf.append((cp.index[-1], float(cp["equity"].iloc[-1])))
    mwr = xirr(sorted(cf))
    return dict(total_return=total, cagr=cagr, vol=sd * math.sqrt(A) if sd else None, sharpe=sharpe,
                sortino=sortino, max_dd_pct=max_dd_pct, calmar=calmar, mwr=mwr, n_days=n,
                calendar_days=cal_days, short_history=n < cfg["min_days_for_annualised"])


# ---------------------------------------------------------------- concentration
def concentration(values: pd.Series, ns=(5, 10)) -> pd.DataFrame:
    """Dependence on the best N outcomes: share of total and total excluding them (hypothetical)."""
    v = pd.Series(values, dtype=float).dropna().sort_values(ascending=False)
    tot = float(v.sum())
    rows = []
    for k in ns:
        top = float(v.head(k).sum())
        rows.append(dict(top_n=k, top_sum=top, share_of_total=(top / tot) if tot > 0 else None,
                         total_excluding=tot - top, available=len(v) >= k))
    return pd.DataFrame(rows)
