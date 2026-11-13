"""ESTIMATED transaction charges (hypothetical). Source P&L is gross; this never replaces it."""
from __future__ import annotations

import numpy as np
import pandas as pd


def estimate_charges(legs: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Per-leg estimated charges for filled legs: entry order + exit order.

    No exit order is charged for EXPIRY_SETTLED legs. Cancelled/rejected legs cost 0.
    Formula per order (premium turnover T = price × qty):
      brokerage flat + STT(sell side) + exchange txn % + SEBI fee + stamp(buy side) + GST on (brok+exch+SEBI)
    """
    brok = float(cfg.get("brokerage_per_order_inr", 0))
    stt = float(cfg.get("stt_sell_pct", 0)) / 100
    ex_nse = float(cfg.get("exchange_txn_pct_nse", 0)) / 100
    ex_bse = float(cfg.get("exchange_txn_pct_bse", 0)) / 100
    sebi = float(cfg.get("sebi_fee_per_crore_inr", 0)) / 1e7
    stamp = float(cfg.get("stamp_duty_buy_pct", 0)) / 100
    gst = float(cfg.get("gst_pct", 0)) / 100

    filled = legs["entry_status"].eq("COMPLETE").to_numpy()
    qty = legs["quantity"].fillna(0).to_numpy()
    t_entry = legs["average_entry_price"].fillna(0).to_numpy() * qty
    has_exit = filled & ~legs["exit_type"].eq("EXPIRY_SETTLED").to_numpy()
    t_exit = np.where(has_exit, legs["exit_average_price"].fillna(0).to_numpy() * qty, 0.0)
    buy_first = legs["action"].eq("BUY").to_numpy()
    ex = np.where(legs["exchange"].eq("BFO").to_numpy(), ex_bse, ex_nse)

    sell_turn = np.where(buy_first, t_exit, t_entry)
    buy_turn = np.where(buy_first, t_entry, t_exit)
    n_orders = filled.astype(int) + has_exit.astype(int)
    brokerage = brok * n_orders
    exch = ex * (t_entry + t_exit)
    sebi_fee = sebi * (t_entry + t_exit)
    total = brokerage + stt * sell_turn + exch + sebi_fee + stamp * buy_turn + gst * (brokerage + exch + sebi_fee)
    return pd.DataFrame({"est_charges": np.where(filled, total, 0.0)}, index=legs.index)
