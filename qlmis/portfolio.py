"""Cross-system analytics: comparison, contributions, correlations, joint losses, leave-one-out."""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import drawdown as D
from . import metrics as M


def system_daily_matrix(legs: pd.DataFrame, obs_days: pd.Index) -> pd.DataFrame:
    """Observed days × systems daily P&L (0 = no closes that day; days are observed for all)."""
    if legs.empty:
        return pd.DataFrame(index=obs_days)
    m = legs.pivot_table(index="close_date", columns="system_key", values="pnl_basis", aggfunc="sum")
    return m.reindex(obs_days).fillna(0.0)


def path_stats(daily: pd.Series, tol: float) -> dict:
    """Depth and duration stats for one daily P&L series on observed days."""
    if daily.empty:
        return dict(max_dd=np.nan, longest_uw_days=np.nan, current_dd=np.nan, current_uw_days=np.nan,
                    pct_underwater=np.nan, recovery_factor=np.nan)
    inp = D.DDInput(list(daily.index), daily.cumsum().to_numpy(), 0.0,
                    daily.index[0] - pd.Timedelta(days=1))
    ep = D.episodes(inp, tol)
    uw = D.underwater_series(inp, tol)
    sm = D.summarise(ep, uw)
    total = float(daily.sum())
    cur = sm["current"]
    return dict(
        max_dd=sm["max_dd"],
        longest_uw_days=(sm["longest"]["underwater_days"] if sm["longest"] else 0),
        current_dd=(cur["current_depth"] if cur else 0.0),
        current_uw_days=(cur["underwater_days"] if cur else 0),
        pct_underwater=sm["pct_time_underwater"],
        recovery_factor=(total / abs(sm["max_dd"])) if sm["max_dd"] else None,
    )


def comparison(legs: pd.DataFrame, mat: pd.DataFrame, labels: dict, tol: float, be_tol: float,
               group_col: str = "system_key") -> pd.DataFrame:
    """One row per system (or account): money + time-based drawdown side by side."""
    rows = []
    total_all = float(legs["pnl_basis"].sum()) if not legs.empty else 0.0
    for key, g in legs.groupby(group_col):
        st = M.outcome_stats(g.sort_values(["close_date", "entry_ts"])["pnl_basis"], be_tol)
        daily = (mat[key] if group_col == "system_key" and key in mat else
                 mat[[c for c in mat.columns if c.split("|")[0] == key]].sum(axis=1))
        ps = path_stats(daily, tol)
        active = g["close_date"].nunique()
        rows.append({
            "key": key, "name": labels.get(key, key), "net_pnl": st["total"],
            "contribution": (st["total"] / total_all) if total_all else None,
            "legs": st["n"], "active_days": active,
            "win_rate": st["win_rate"], "profit_factor": st["profit_factor"], "payoff": st["payoff"],
            "expectancy": st["expectancy"], "max_dd": ps["max_dd"],
            "longest_underwater_days": ps["longest_uw_days"],
            "current_dd": ps["current_dd"], "current_underwater_days": ps["current_uw_days"],
            "pct_time_underwater": ps["pct_underwater"], "recovery_factor": ps["recovery_factor"],
            "first_close": g["close_date"].min(), "last_close": g["close_date"].max(),
            "sizes": ", ".join(str(int(q)) for q in sorted(g["quantity"].unique())),
        })
    return pd.DataFrame(rows)


def correlations(mat: pd.DataFrame, min_obs: int = 20, active_only: bool = False) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Pairwise Pearson correlation of daily P&L plus observation counts.

    active_only=True uses only days where BOTH systems closed something (non-zero), otherwise all
    observed days (no-trade days count as 0). Pairs with < min_obs observations -> NaN.
    """
    cols = list(mat.columns)
    corr = pd.DataFrame(np.nan, index=cols, columns=cols)
    nobs = pd.DataFrame(0, index=cols, columns=cols)
    for i, a in enumerate(cols):
        for b in cols[i:]:
            x, y = mat[a], mat[b]
            if active_only:
                m = (x != 0) & (y != 0)
                x, y = x[m], y[m]
            n = len(x)
            nobs.loc[a, b] = nobs.loc[b, a] = n
            if n >= min_obs and x.std() > 0 and y.std() > 0:
                corr.loc[a, b] = corr.loc[b, a] = float(np.corrcoef(x, y)[0, 1])
    return corr, nobs


def joint_losses(mat: pd.DataFrame) -> pd.DataFrame:
    """Pairwise count of days where both systems lost, and their combined loss on those days."""
    cols = list(mat.columns)
    rows = []
    for i, a in enumerate(cols):
        for b in cols[i + 1:]:
            m = (mat[a] < 0) & (mat[b] < 0)
            la, lb = int((mat[a] < 0).sum()), int((mat[b] < 0).sum())
            rows.append(dict(a=a, b=b, joint_loss_days=int(m.sum()), a_loss_days=la, b_loss_days=lb,
                             joint_loss_inr=float((mat.loc[m, a] + mat.loc[m, b]).sum())))
    return pd.DataFrame(rows).sort_values("joint_loss_inr") if rows else pd.DataFrame()


def breadth_of_losses(mat: pd.DataFrame) -> pd.DataFrame:
    """Per day: how many systems lost simultaneously, and the basket result."""
    return pd.DataFrame({"systems_losing": (mat < 0).sum(axis=1), "systems_active": (mat != 0).sum(axis=1),
                         "basket_pnl": mat.sum(axis=1)})


def worst_dd_contributions(mat: pd.DataFrame, tol: float, top: int = 3) -> tuple[pd.DataFrame, pd.DataFrame]:
    """System P&L inside the basket's deepest drawdown episodes (peak→trough)."""
    basket = mat.sum(axis=1)
    if basket.empty:
        return pd.DataFrame(), pd.DataFrame()
    inp = D.DDInput(list(basket.index), basket.cumsum().to_numpy(), 0.0, basket.index[0] - pd.Timedelta(days=1))
    ep = D.episodes(inp, tol).sort_values("depth").head(top).reset_index(drop=True)
    contrib = {}
    for i, r in ep.iterrows():
        w = (mat.index > r["peak_date"]) & (mat.index <= r["trough_date"])
        contrib[f"DD#{i+1} ({r['peak_date']}→{r['trough_date']})"] = mat.loc[w].sum()
    return ep, pd.DataFrame(contrib)


def leave_one_out(mat: pd.DataFrame, labels: dict, tol: float) -> pd.DataFrame:
    """Basket results with each system removed (hypothetical, same observed days)."""
    rows = []
    full = path_stats(mat.sum(axis=1), tol)
    rows.append(dict(excluded="(none — full basket)", net_pnl=float(mat.values.sum()), **full))
    for c in mat.columns:
        ps = path_stats(mat.drop(columns=[c]).sum(axis=1), tol)
        rows.append(dict(excluded=labels.get(c, c), net_pnl=float(mat.drop(columns=[c]).values.sum()), **ps))
    df = pd.DataFrame(rows)
    df["Δ net vs full"] = df["net_pnl"] - df["net_pnl"].iloc[0]
    df["Δ max DD vs full"] = df["max_dd"] - df["max_dd"].iloc[0]
    return df


def sizing_changes(legs: pd.DataFrame, labels: dict) -> pd.DataFrame:
    """Detect quantity changes per system over time (first date each size appeared)."""
    rows = []
    for k, g in legs.sort_values("entry_ts").groupby("system_key"):
        prev = None
        for d, q in zip(g["close_date"], g["quantity"]):
            if q != prev:
                if prev is not None:
                    rows.append(dict(system=labels.get(k, k), date=d, from_qty=int(prev), to_qty=int(q)))
                prev = q
    return pd.DataFrame(rows)
