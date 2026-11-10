"""Share codes, compatible with Algo Lab's envelope: ALGOSHARE1.<base64url(zlib(json))>.<check>.

TradeDesk shares carry kind "spec" (a whole no-code or python strategy spec) and, if chosen, a short results
summary. Algo Lab shares (kind "preset": a built-in strategy and its settings) import as a python strategy when
the server knows that strategy. A share never carries logins, keys, account details or positions."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import zlib

PREFIX = "ALGOSHARE1"
MAX_CODE = 40_000
SUMMARY_KEYS = ("net_pnl", "gross_pnl", "charges", "trades", "win_rate_pct", "max_drawdown", "profit_factor",
                "from", "to", "fill_model")


def _check(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()[:8]


def encode(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    body = base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode().rstrip("=")
    return f"{PREFIX}.{body}.{_check(body)}"


def decode(code: str) -> dict:
    text = re.sub(r"\s+", "", str(code or ""))
    if text.startswith("{"):
        try:
            return json.loads(code)
        except ValueError:
            raise ValueError("That file is not a strategy share.") from None
    m = re.search(PREFIX + r"\.([A-Za-z0-9_-]+)\.([0-9a-f]{8})", text)
    if not m or len(text) > MAX_CODE:
        raise ValueError("That is not a share code: it starts with ALGOSHARE1.")
    body, chk = m.groups()
    if _check(body) != chk:
        raise ValueError("The share code is incomplete or changed: copy it again in full.")
    try:
        return json.loads(zlib.decompress(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)),
                                          bufsize=MAX_CODE * 8).decode())
    except (ValueError, zlib.error, UnicodeDecodeError):
        raise ValueError("The share code could not be read.") from None


def make_payload(share_id: str, version: int, spec: dict, by: str, results: dict | None) -> dict:
    p = {"v": 1, "kind": "spec", "id": share_id, "version": version, "title": spec.get("name", "")[:80],
         "from": by[:40], "spec": spec}
    if results:
        p["backtest"] = {k: results.get(k) for k in SUMMARY_KEYS if k in results}
    return p


def diff(old: dict, new: dict, prefix: str = "") -> list[dict]:
    """Settings that differ between two specs, as dotted paths."""
    out = []
    keys = sorted(set(old) | set(new)) if isinstance(old, dict) and isinstance(new, dict) else []
    if not keys:
        return [] if old == new else [{"path": prefix or "(all)", "from": old, "to": new}]
    for k in keys:
        a, b = old.get(k), new.get(k)
        path = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(a, dict) and isinstance(b, dict):
            out += diff(a, b, path)
        elif a != b:
            out.append({"path": path, "from": a, "to": b})
    return out
