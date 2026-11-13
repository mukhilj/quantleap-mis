"""Hand-checkable tests for financially consequential calculations."""
import math
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from qlmis import drawdown as D
from qlmis import metrics as M
from qlmis.coverage import combined_status, coverage_notice, segments
from qlmis.data import Selection, selected_systems, parse_symbol
from qlmis.fmt import inr

TOL = 1.0


def path(values, start=date(2026, 1, 5)):
    """Cumulative path on consecutive calendar days."""
    dates = [start + timedelta(days=i) for i in range(len(values))]
    return D.DDInput(dates, np.array(values, float), 0.0, start - timedelta(days=1))


# ---------------------------------------------------------------- outcome stats
def test_win_loss_breakeven_and_ratios():
    s = M.outcome_stats(pd.Series([100, -50, 0, 200, -50]))
    assert (s["wins"], s["losses"], s["breakevens"], s["n"]) == (2, 2, 1, 5)
    assert s["win_rate"] == pytest.approx(0.4)            # breakeven in denominator
    assert s["win_rate_decided"] == pytest.approx(0.5)
    assert s["avg_win"] == 150 and s["avg_loss"] == -50
    assert s["payoff"] == pytest.approx(3.0)              # 150 / 50
    assert s["profit_factor"] == pytest.approx(3.0)       # 300 / 100
    assert s["expectancy"] == pytest.approx(0.4 * 150 - 0.4 * 50)  # = 40 = mean
    assert s["expectancy"] == pytest.approx(np.mean([100, -50, 0, 200, -50]))


def test_breakeven_tolerance():
    s = M.outcome_stats(pd.Series([0.4, -0.4, 10]), be_tol=0.5)
    assert (s["wins"], s["losses"], s["breakevens"]) == (1, 0, 2)


def test_no_losses_and_no_wins_are_not_zero():
    s = M.outcome_stats(pd.Series([10, 20]))
    assert s["profit_factor"] == math.inf and s["payoff"] is None and s["avg_loss"] is None
    s = M.outcome_stats(pd.Series([-10, -20]))
    assert s["profit_factor"] == 0 and s["payoff"] is None and s["avg_win"] is None
    s = M.outcome_stats(pd.Series([], dtype=float))
    assert s["n"] == 0 and s["win_rate"] is None and s["profit_factor"] is None


def test_streaks_breakeven_resets():
    s = M.outcome_stats(pd.Series([1, 1, 1, 0, 1, -1, -1, 1, -1, -1, -1]))
    assert s["max_win_streak"] == 3 and s["max_loss_streak"] == 3 and s["current_streak"] == -3


# ---------------------------------------------------------------- drawdowns
def test_first_observation_loss_is_drawdown():
    inp = path([-100, -50, 10])
    ep = D.episodes(inp, TOL)
    assert len(ep) == 1
    r = ep.iloc[0]
    assert r["depth"] == -100 and r["peak_note"] == "start of observed history"
    assert r["peak_date"] == date(2026, 1, 4) and r["recovery_date"] == date(2026, 1, 7)
    assert r["status"] == "Recovered"


def test_deep_short_vs_shallow_long_are_different_episodes():
    # deep short: -1000 then recovered next day; shallow long: -10 for 30 days, ongoing
    vals = [100, -900, 200] + [190] * 30
    inp = path(vals)
    ep = D.episodes(inp, TOL)
    sm = D.summarise(ep, D.underwater_series(inp, TOL))
    assert sm["deepest"]["depth"] == -1000 and sm["deepest"]["underwater_days"] == 2
    assert sm["longest"]["depth"] == -10 and sm["longest"]["status"] == "Ongoing"
    assert sm["longest"]["underwater_days"] == 30           # peak day 3 -> last obs day 33
    assert sm["longest"]["underwater_sessions"] == 30
    assert sm["current"]["current_depth"] == -10
    # recovery duration of deepest: trough day 2 -> recovery day 3
    assert sm["deepest"]["trough_to_recovery_days"] == 1


def test_recovery_tolerance_and_new_high():
    inp = path([100, 50, 99.5, 99.5, 101])
    ep = D.episodes(inp, TOL)
    assert len(ep) == 1 and ep.iloc[0]["recovery_date"] == date(2026, 1, 7)  # 99.5 within ₹1
    st = D.stagnation(inp)
    # no new high from day1 (100) until day5 (101): 4 calendar days, 3 sessions in between
    assert st["longest_days"] == 4 and st["longest_sessions"] == 3


def test_ongoing_has_no_recovery_date():
    ep = D.episodes(path([100, 40, 60]), TOL)
    assert ep.iloc[0]["status"] == "Ongoing" and ep.iloc[0]["recovery_date"] is None
    assert np.isnan(ep.iloc[0]["trough_to_recovery_days"])
    assert ep.iloc[0]["underwater_days"] == 2


def test_carry_forward_peak_across_filter_boundary():
    # full history: up to 500 then down to 300 before window; window continues 300 -> 400
    days = [date(2026, 1, 1) + timedelta(days=i) for i in range(6)]
    full = pd.DataFrame({"pnl": [200, 300, -100, -100, 50, 50]}, index=days)
    prior, window = full.iloc[:4], full.iloc[4:]
    reset = D.episodes(M.dd_input_from_daily(window, mode="reset"), TOL)
    assert reset.empty                                     # window alone only rises
    inp = M.dd_input_from_daily(window, prior=prior, mode="carry")
    ep = D.episodes(inp, TOL)
    assert len(ep) == 1 and ep.iloc[0]["peak_date"] == days[1]
    assert ep.iloc[0]["peak_note"].startswith("peak before window")
    assert ep.iloc[0]["status"] == "Ongoing"


def test_pct_depth_uses_level_at_peak():
    inp = path([100, -100])
    level = 1000 + inp.values
    ep = D.episodes(inp, TOL, level=level)
    assert ep.iloc[0]["depth_pct"] == pytest.approx(-200 / 1100)


def test_pct_time_underwater():
    inp = path([10, 5, 5, 20])
    sm = D.summarise(D.episodes(inp, TOL), D.underwater_series(inp, TOL))
    assert sm["pct_time_underwater"] == pytest.approx(0.5)


# ---------------------------------------------------------------- coverage & missing data
def test_missing_days_are_nan_not_zero():
    days = [date(2026, 1, 5) + timedelta(days=i) for i in range(4)]
    status = pd.Series(["covered", "missing", "covered", "partial"], index=days)
    legs = pd.DataFrame({"close_date": [days[0], days[2]], "pnl_basis": [100.0, -30.0]})
    d = M.daily_series(legs, status)
    assert d.loc[days[1], "pnl"] != d.loc[days[1], "pnl"]  # NaN
    assert d.loc[days[3], "pnl"] == 0.0                     # partial day, no closes yet
    obs = M.observed(d)
    assert list(obs.index) == [days[0], days[2], days[3]]


def test_unequal_coverage_combined_status():
    days = [date(2026, 1, 5) + timedelta(days=i) for i in range(3)]
    ds = pd.DataFrame({"A": ["covered", "covered", "covered"], "B": ["missing", "covered", "partial"]}, index=days)
    st = combined_status(ds)
    assert list(st) == ["missing", "covered", "partial"]
    n = coverage_notice(st, date(2025, 12, 1), days[-1])
    assert n["observed"] == (days[1], days[2]) and n["missing"] == [(days[0], days[0])]
    assert not n["complete"]


def test_period_table_flags_partial_and_missing():
    days = [d.date() for d in pd.bdate_range("2026-01-05", "2026-01-16")]
    st = ["missing"] * 5 + ["covered"] * 4 + ["partial"]
    daily = pd.DataFrame({"status": st, "pnl": [np.nan] * 5 + [10, 10, -5, 0, 3], "n_closes": [0] * 5 + [1] * 5},
                         index=days)
    wk = M.period_table(M.observed(daily), "W", daily)
    assert wk.iloc[0]["completeness"] == "missing" and np.isnan(wk.iloc[0]["pnl"])
    assert wk.iloc[1]["completeness"] == "partial (incomplete day)" and wk.iloc[1]["pnl"] == 18


# ---------------------------------------------------------------- capital & returns
def _obs(pnls, start=date(2026, 1, 5)):
    days = [start + timedelta(days=i) for i in range(len(pnls))]
    return pd.DataFrame({"pnl": pnls, "status": "covered"}, index=days)


def test_deposit_is_not_profit_and_twr():
    obs = _obs([100.0, 0.0, 110.0])
    flows = pd.DataFrame({"date": [date(2026, 1, 1), date(2026, 1, 6)], "client_id": ["A", "A"],
                          "signed_amount": [1000.0, 1100.0]})
    cp = M.capital_path(obs, flows, ["A"])
    assert list(cp["start_equity"]) == [1000, 2200, 2200]
    assert cp["ret"].iloc[0] == pytest.approx(0.10) and cp["ret"].iloc[2] == pytest.approx(0.05)
    assert cp["equity"].iloc[-1] == pytest.approx(2310)     # 2100 deposits + 210 P&L
    assert cp["twr_index"].iloc[-1] == pytest.approx(1.10 * 1.05)


def test_internal_transfer_nets_out_at_basket_level():
    obs = _obs([0.0, 50.0])
    flows = pd.DataFrame({"date": [date(2026, 1, 1)] * 2 + [date(2026, 1, 6)] * 2,
                          "client_id": ["A", "B", "A", "B"],
                          "signed_amount": [1000.0, 1000.0, -300.0, 300.0]})
    cp = M.capital_path(obs, flows, ["A", "B"])
    assert list(cp["flow"]) == [2000.0, 0.0]


def test_return_metrics_none_without_capital():
    r = M.return_metrics(None, {"annualisation_days": 252})
    assert all(v is None for v in r.values())


def test_xirr_simple():
    r = M.xirr([(date(2025, 1, 1), -1000.0), (date(2026, 1, 1), 1100.0)])
    assert r == pytest.approx(0.10, abs=2e-3)


# ---------------------------------------------------------------- selection & misc
def test_overlapping_baskets_include_each_system_once():
    allsys = {"A|1", "A|2", "B|3"}
    baskets = {"x": {"A|1", "A|2"}, "y": {"A|2", "B|3"}}
    keys = selected_systems(allsys, Selection(date.min, date.max, baskets=["x", "y"]), baskets)
    assert keys == allsys
    keys = selected_systems(allsys, Selection(date.min, date.max, baskets=["x", "y"], accounts=["A"]), baskets)
    assert keys == {"A|1", "A|2"}


def test_symbol_parsing():
    assert parse_symbol("NIFTY2681824300PE") == ("NIFTY", 24300.0, "PE")
    assert parse_symbol("BANKNIFTY26AUG58900CE") == ("BANKNIFTY", 58900.0, "CE")
    assert parse_symbol("NIFTY26O0623350CE") == ("NIFTY", 23350.0, "CE")
    assert parse_symbol("SENSEX26N1282300PE") == ("SENSEX", 82300.0, "PE")


def test_indian_format():
    assert inr(12345678) == "₹1,23,45,678" and inr(-1500.5, 2) == "−₹1,500.50"


def test_concentration():
    c = M.concentration(pd.Series([50, 30, 10, -40]), ns=(1, 2))
    assert c.iloc[0]["share_of_total"] == pytest.approx(50 / 50)
    assert c.iloc[1]["total_excluding"] == pytest.approx(-30)
