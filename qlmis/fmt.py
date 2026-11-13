"""Indian number formatting helpers."""
from __future__ import annotations

import math


def _group_indian(n: int) -> str:
    # 12345678 -> 1,23,45,678
    s = str(n)
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts) + "," + tail


def inr(x, decimals: int = 0, sign: bool = False) -> str:
    """₹ with Indian digit grouping. None/NaN -> '—'."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    neg = x < 0
    v = round(abs(float(x)), decimals)
    whole = int(v)
    frac = f"{v - whole:.{decimals}f}"[1:] if decimals else ""
    s = f"₹{_group_indian(whole)}{frac}"
    return ("−" if neg else ("+" if sign and x > 0 else "")) + s


def lakh(x) -> str:
    """Compact: ₹12.3 L / ₹1.23 Cr."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    a = abs(x)
    t = f"₹{a/1e7:.2f} Cr" if a >= 1e7 else (f"₹{a/1e5:.2f} L" if a >= 1e5 else inr(a))
    return ("−" if x < 0 else "") + t


def pct(x, decimals: int = 1) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{x*100:.{decimals}f}%"


def num(x, decimals: int = 2) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "—" if x is None or math.isnan(x) else ("∞" if x > 0 else "−∞")
    return f"{x:,.{decimals}f}"
