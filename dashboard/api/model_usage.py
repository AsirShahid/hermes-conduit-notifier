"""How much Asir uses each model, for Favorites in Conduit's model sheet.

Asir, 2026-11-15: Favorites should start as "the 4-5 that I use the most often ... biased towards used most often more
recently", counted "like the same thing we use for usage.asir.dev, sorting by request", so models he stopped using a
few weeks ago (GPT-6 Luna, GPT-5.6 Luna and Sol) drop out however much he used them before.

The count is Hermes' own per-model request ledger, ``session_model_usage`` (what usage.asir.dev's Hermes collector
reads): every request a model made for the profile, with the provider that served it, so a fallback turn counts for
the fallback's provider and only for its own few requests. Auxiliary work (titles, compression, vision, approvals,
background review) is left out: he never picks those models. Each row's requests count in full when used today and
half as much every ``HALF_LIFE_DAYS`` days since, over the last ``WINDOW_DAYS`` days, and a model with no requests in
the last ``RECENT_DAYS`` days is left out however much it was used before (50,000 GPT-5.6 Sol requests from 40 days
ago would otherwise outweigh 600 Fable requests this week). Rows are grouped by (provider, model); Conduit matches
them to the providers its model sheet lists, which only it knows.
"""

from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import HTTPException, Response

from . import core

router = core.router

WINDOW_DAYS = 90
HALF_LIFE_DAYS = 7
RECENT_DAYS = 14
MAX_ROWS = 60
USAGE_LIMIT = 30
USAGE_WINDOW_S = 60
_REQUIRED = ("model", "billing_provider", "task", "api_call_count", "last_seen")

_limiter = core._MintLimiter(USAGE_LIMIT, USAGE_WINDOW_S, message="Too many model usage requests; try again shortly")
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="conduit-model-usage")


def state_db() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "state.db"


def usage(path: Optional[Path] = None, now: Optional[float] = None) -> Dict[str, Any]:
    path = Path(path) if path is not None else state_db()
    now = time.time() if now is None else now
    result: Dict[str, Any] = {"window_days": WINDOW_DAYS, "half_life_days": HALF_LIFE_DAYS, "recent_days": RECENT_DAYS, "rows": []}
    if not path.exists():
        return result
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(session_model_usage)")}
        if not columns:
            return result
        if not all(name in columns for name in _REQUIRED):
            raise core.TokenError(501, "This Hermes doesn't record what Conduit needs for model usage; update Hermes")
        rows = conn.execute(
            "SELECT model, billing_provider, api_call_count, last_seen FROM session_model_usage "
            "WHERE COALESCE(task, '') = '' AND model IS NOT NULL AND model <> '' AND api_call_count > 0 AND last_seen >= ?",
            (now - WINDOW_DAYS * 86400.0,)).fetchall()
    finally:
        conn.close()
    groups: Dict[tuple, Dict[str, Any]] = {}
    for model, provider, requests, seen in rows:
        try:
            seen, requests = float(seen), int(requests)
        except (TypeError, ValueError):
            continue
        provider = provider.strip() if isinstance(provider, str) and provider.strip() else None
        key = (provider, model.strip())
        group = groups.setdefault(key, {"provider": key[0], "model": key[1], "weight": 0.0, "requests": 0, "latest": 0})
        group["weight"] += requests * 0.5 ** (max(0.0, now - seen) / 86400.0 / HALF_LIFE_DAYS)
        group["requests"] += requests
        group["latest"] = max(group["latest"], int(seen * 1000))
    recent = int((now - RECENT_DAYS * 86400.0) * 1000)
    ranked = sorted((g for g in groups.values() if g["latest"] >= recent), key=lambda g: (-g["weight"], -g["latest"]))[:MAX_ROWS]
    for group in ranked:
        group["weight"] = round(group["weight"], 4)
    result["rows"] = ranked
    return result


@router.get("/model-usage")
async def get_model_usage(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    no_store = {"Cache-Control": "no-store"}
    try:
        return {"ok": True, **(await core._run_scoped(profile, core._limited(_limiter, profile, lambda: usage()), _executor))}
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers=no_store)
    except sqlite3.Error as exc:
        raise core._unexpected("model usage", exc, feature="Model usage")
