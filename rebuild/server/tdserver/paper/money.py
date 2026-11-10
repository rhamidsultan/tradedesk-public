"""Rupees in the Indian format (₹1,25,000) with a true minus sign, for alerts and messages."""
from __future__ import annotations


def inr(v: float, decimals: int = 0, signed: bool = False) -> str:
    neg = v < 0
    s = f"{abs(v):.{decimals}f}"
    whole, _, frac = s.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    out = "₹" + whole + (f".{frac}" if frac else "")
    if neg and float(s) != 0:
        return "−" + out
    return ("+" + out) if signed and v > 0 else out
