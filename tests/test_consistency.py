"""Filter consistency and export agreement on the real ledger."""
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from qlmis import portfolio as P
from qlmis import report as R
from qlmis.config import load_capital, load_holidays, load_settings
from qlmis.coverage import account_intervals
from qlmis.data import Selection, load_baskets, load_legs
from qlmis.db import connect

S = load_settings()
pytestmark = pytest.mark.skipif(not S.database.exists(), reason="ledger not imported yet")


@pytest.fixture(scope="module")
def env():
    con = connect(S.database)
    hol = load_holidays(S)
    out = (load_legs(con, S, hol), load_baskets(con), account_intervals(con), hol, load_capital(S))
    con.close()
    return out


def ctx_for(env, **kw):
    legs, b, iv, hol, cap = env
    return R.build(legs, Selection(date(2026, 1, 1), date(2026, 12, 31), **kw), b, iv, hol, cap, S.calc)


def test_accounts_sum_to_combined(env):
    full = ctx_for(env)
    parts = [ctx_for(env, accounts=[a]).unit_stats["total"] for a in sorted(env[0].client_id.unique())]
    assert sum(parts) == pytest.approx(full.unit_stats["total"])
    assert full.unit_stats["n"] == sum(ctx_for(env, accounts=[a]).unit_stats["n"] for a in sorted(env[0].client_id.unique()))


def test_systems_sum_to_account(env):
    acct = sorted(env[0].client_id.unique())[0]
    c = ctx_for(env, accounts=[acct])
    mat = P.system_daily_matrix(c.legs, c.obs.index)
    assert mat.values.sum() == pytest.approx(c.unit_stats["total"])
    assert c.obs["pnl"].sum() == pytest.approx(c.unit_stats["total"])


def test_runs_and_legs_same_total(env):
    assert ctx_for(env, unit="run").unit_stats["total"] == pytest.approx(ctx_for(env).unit_stats["total"])


def test_overlapping_baskets_no_double_count(env):
    legs, b, *_ = env
    names = [n for n in b if n.startswith("QL")]
    allb = ctx_for(env, baskets=names)
    assert allb.unit_stats["total"] == pytest.approx(ctx_for(env).unit_stats["total"])
    one = names[0]
    twice = ctx_for(env, baskets=[one, one])
    assert twice.unit_stats["n"] == ctx_for(env, baskets=[one]).unit_stats["n"]


def test_export_matches_context(env):
    c = ctx_for(env)
    h = R.headline_table(c).set_index("metric")["value"]
    assert h["Total P&L"] == c.unit_stats["total"]
    assert h["Max drawdown (₹)"] == c.dd_summary["max_dd"]
    assert h["Win rate (wins/all)"] == c.unit_stats["win_rate"]
    assert isinstance(h["Total P&L"], float)          # numeric types preserved


def test_subset_has_no_pct_metrics(env):
    legs = env[0]
    k = legs[legs.filled].system_key.iloc[0]
    c = ctx_for(env, systems=[k])
    assert c.capital_path is None and c.returns["cagr"] is None


def test_source_reconciliation(env):
    """Ledger confirmed totals equal the sum of the original CSVs' filled rows."""
    from qlmis.ingest import discover
    legs = env[0]
    frames = [pd.read_csv(f, encoding="utf-8-sig", dtype={"client_id": str, "position_id": str})
              for f in discover(S)]                       # oldest file first
    src = pd.concat(frames, ignore_index=True)
    src = src[src.position_id.notna()].drop_duplicates(["client_id", "position_id"], keep="last")
    tot = src.loc[src.entry_status == "COMPLETE", "pnl"].sum()
    assert legs.loc[legs.filled, "pnl"].sum() == pytest.approx(tot)
