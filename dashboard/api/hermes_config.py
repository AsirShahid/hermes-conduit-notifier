"""Hermes configuration: the routes behind Conduit's Hermes page (Asir, 2026-11-15: "put it all behind one specific
Hermes configuration page").

- Built-in memory: MEMORY.md and USER.md, as entries with their budgets, through Hermes' own MemoryStore so its file
  lock, character limits, drift guard and content scan apply. An edit names the whole entry it changes
  (``matched_entry``): one that changed since the phone read it is refused as stale, never applied to another entry.
  The dashboard is the user, so no write-approval gate, as Hermes' own ``/api/skills`` edits.
- OpenViking: browse, search, read and forget through the OpenViking memory provider's own tools (its auth, identity
  and the forget URI check), on the provider memory.py keeps for each profile.
- Hermes files: where the profile's SOUL.md, config.yaml and memory files are, for Conduit's file editor (which
  refuses .env and credential files itself).

Skills need nothing here: Hermes' ``POST /api/skills`` and ``PUT /api/skills/content`` create and edit them.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from fastapi import HTTPException, Request, Response

from . import core, memory

logger = logging.getLogger(__name__)

router = core.router

CONFIG_MAX_BODY_BYTES = 64 * 1024
CONFIG_LIMIT = 120
CONFIG_WINDOW_S = 60.0
VIKING_MAX_QUERY_CHARS = 500
VIKING_MAX_URI_CHARS = 1000
VIKING_READ_MAX_CHARS = 60_000
TARGETS = ("memory", "user")
ACTIONS = ("add", "edit", "remove")
OPENVIKING = "openviking"

# Files a Hermes home keeps that Asir edits by hand, relative to the profile's home.
HERMES_FILES = (
    ("soul", "SOUL.md"),
    ("config", "config.yaml"),
    ("memory", "memories/MEMORY.md"),
    ("user", "memories/USER.md"),
    ("cron", "cron/jobs.json"),
)

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="conduit-hermes-config")
_limiter = core._MintLimiter(CONFIG_LIMIT, CONFIG_WINDOW_S, message="Too many Hermes configuration requests; try again shortly")


# --- Built-in memory ---------------------------------------------------------

def _load_store() -> Any:
    """Hermes' on-disk MemoryStore with the configured limits (a seam for tests)."""
    from tools.memory_tool import load_on_disk_store

    return load_on_disk_store()


def _delimiter() -> str:
    try:
        from tools.memory_tool_store import ENTRY_DELIMITER
        return ENTRY_DELIMITER
    except ImportError:
        return "\n§\n"


def memory_listing(store: Any = None) -> Dict[str, Any]:
    """Each target's entries, characters used (as Hermes counts them, delimiters included) and limit."""
    store = store if store is not None else _load_store()
    delimiter = _delimiter()
    targets = {}
    for target in TARGETS:
        entries = list(store.user_entries if target == "user" else store.memory_entries)
        targets[target] = {
            "enabled": bool(store.target_enabled(target)),
            "entries": entries,
            "used": len(delimiter.join(entries)),
            "limit": int(store.user_char_limit if target == "user" else store.memory_char_limit),
        }
    return {"available": True, "targets": targets}


def change_memory(body: Any, store: Any = None) -> Dict[str, Any]:
    """One add, edit or remove; the listing after it. Refusals (over budget, stale entry, blocked content) are 409/400
    with the store's own message."""
    if not isinstance(body, dict):
        raise core.TokenError(400, "Send a JSON object")
    target, action = body.get("target"), body.get("action")
    if target not in TARGETS:
        raise core.TokenError(400, "target is memory or user")
    if action not in ACTIONS:
        raise core.TokenError(400, "action is add, edit or remove")
    content = body.get("content")
    entry = body.get("entry")
    if action in ("add", "edit") and (not isinstance(content, str) or not content.strip()):
        raise core.TokenError(400, "content is required")
    if action in ("edit", "remove") and (not isinstance(entry, str) or not entry.strip()):
        raise core.TokenError(400, "entry (the whole entry being changed) is required")
    store = store if store is not None else _load_store()
    title = "User profile" if target == "user" else "Memory"
    if not store.target_enabled(target):
        raise core.TokenError(409, f"{title} is turned off in Hermes' config")
    failure = _failure_class()
    if failure is not None:
        failure.set(None)
    if action == "add":
        result = store.add(target, content)
    elif action == "edit":
        result = store.replace(target, entry, content, matched_entry=entry)
    else:
        result = store.remove(target, entry, matched_entry=entry)
    if not isinstance(result, dict) or not result.get("success"):
        message = str((result.get("error") if isinstance(result, dict) else None) or "Hermes refused the change")
        raise core.TokenError(409, _refusal(failure.get() if failure is not None else None, message, title, store, target))
    return {"message": str(result.get("message") or "Saved."), "memory": memory_listing(store)}


def _failure_class() -> Any:
    """The store's failure code (a contextvar it sets beside the error), when this Hermes has one."""
    try:
        from tools.memory_tool_store import FAILURE_CLASS
        return FAILURE_CLASS
    except (ImportError, AttributeError):
        return None


def _refusal(code: Optional[str], message: str, title: str, store: Any, target: str) -> str:
    """The store's refusals are worded for the agent ("retry as one operations batch"): say them for a person."""
    lowered = message.lower()
    if code == "stale_entry" or (code is None and "changed" in lowered):
        return "That entry changed on the server after this page loaded, so nothing was changed. Showing the current entries."
    if code == "over_budget" or (code is None and "limit" in lowered):
        limit = int(store.user_char_limit if target == "user" else store.memory_char_limit)
        return f"That would put {title} over its {limit:,}-character limit. Shorten it, or remove another entry first."
    if code == "scan_blocked" or lowered.startswith("blocked:"):
        detail = message.split(":", 1)[1].split(".")[0].strip() if ":" in message else message
        return f"Hermes blocked this text ({detail}): {title} goes into every prompt."
    return message


# --- Hermes files --------------------------------------------------------------

def hermes_files() -> Dict[str, Any]:
    from hermes_constants import get_hermes_home

    home = Path(get_hermes_home())
    files = []
    for key, relative in HERMES_FILES:
        path = home / relative
        try:
            size = path.stat().st_size if path.is_file() else None
        except OSError:
            size = None
        files.append({"key": key, "path": str(path), "exists": size is not None, "size": size})
    return {"home": str(home), "files": files}


# --- OpenViking ---------------------------------------------------------------

def _viking_provider(key: str, deadline: float) -> Any:
    """The profile's OpenViking provider entry (memory.py's cache), or None when another provider (or none) is set."""
    name = memory._configured_memory_provider(memory._hermes_memory_config())
    if (name or "").strip().lower() != OPENVIKING:
        return None
    return memory._memory_providers.get(key, name, deadline)


def _tool(entry: Any, tool: str, args: Dict[str, Any], deadline: float) -> Dict[str, Any]:
    provider = entry.provider
    raw = entry.call(lambda: provider.handle_tool_call(tool, args), deadline, tool.removeprefix("viking_"))
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        raise core.TokenError(502, "OpenViking answered something Conduit can't read")
    if not isinstance(parsed, dict):
        raise core.TokenError(502, "OpenViking answered something Conduit can't read")
    if parsed.get("error"):
        raise core.TokenError(409, str(parsed["error"])[:500])
    return parsed


def _uri(value: Any, *, default: str = "") -> str:
    uri = (value if isinstance(value, str) else "").strip() or default
    if not uri.startswith("viking://") or len(uri) > VIKING_MAX_URI_CHARS or any(c in uri for c in "\r\n\0"):
        raise core.TokenError(400, "uri is a viking:// address")
    return uri


def viking(action: str, params: Dict[str, Any], key: str = "") -> Dict[str, Any]:
    """list (a folder), search (a query, optionally within a folder), read (one memory, in full), forget (one memory
    file)."""
    import time

    deadline = time.monotonic() + memory.MEMORY_TIMEOUT_S
    try:
        entry = _viking_provider(key, deadline)
    except ImportError as exc:
        if not memory._is_missing_memory_modules(exc):
            raise
        entry = None
    if entry is None:
        return {"available": False}
    if action == "list":
        result = _tool(entry, "viking_browse", {"action": "list", "path": _uri(params.get("uri"), default="viking://")}, deadline)
        return {"available": True, "path": result.get("path"), "entries": result.get("entries") or []}
    if action == "search":
        query = " ".join(str(params.get("q") or "").split())
        if not query:
            raise core.TokenError(400, "q is required")
        if len(query) > VIKING_MAX_QUERY_CHARS:
            raise core.TokenError(400, f"q is longer than {VIKING_MAX_QUERY_CHARS} characters")
        args: Dict[str, Any] = {"query": query, "limit": 30}
        if params.get("uri"):
            args["scope"] = _uri(params.get("uri"))
        result = _tool(entry, "viking_search", args, deadline)
        return {"available": True, "results": result.get("results") or [], "total": result.get("total")}
    if action == "read":
        uri = _uri(params.get("uri"))
        result = _tool(entry, "viking_read", {"uri": uri, "level": "full"}, deadline)
        content = str(result.get("content") or "")
        return {"available": True, "uri": uri, "content": content[:VIKING_READ_MAX_CHARS],
                "truncated": len(content) > VIKING_READ_MAX_CHARS}
    if action == "forget":
        uri = _uri(params.get("uri"))
        result = _tool(entry, "viking_forget", {"uri": uri}, deadline)
        return {"available": True, "status": result.get("status"), "uri": result.get("uri") or uri}
    raise core.TokenError(400, "action is list, search, read or forget")


# --- Routes ----------------------------------------------------------------------

async def _run(profile: Optional[str], fn: Callable[[], Dict[str, Any]], what: str,
               executor: Optional[ThreadPoolExecutor] = None) -> Dict[str, Any]:
    no_store = {"Cache-Control": "no-store"}
    try:
        return {"ok": True, **(await core._run_scoped(profile, core._limited(_limiter, profile, fn), executor or _executor))}
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers=no_store)
    except HTTPException as exc:
        exc.headers = {**(exc.headers or {}), **no_store}
        raise
    except ImportError as exc:
        if memory._is_missing_memory_modules(exc) or exc.name in ("tools", "tools.memory_tool", "hermes_constants"):
            raise HTTPException(status_code=501, detail="This Hermes doesn't offer that; update Hermes", headers=no_store)
        raise core._unexpected(what, exc, feature="Hermes configuration")
    except Exception as exc:
        raise core._unexpected(what, exc, feature="Hermes configuration")


@router.get("/hermes-config/memory")
async def get_hermes_memory(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return await _run(profile, memory_listing, "memory")


@router.post("/hermes-config/memory")
async def post_hermes_memory(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    body = await core._read_json_body(request, CONFIG_MAX_BODY_BYTES)
    return await _run(profile, lambda: change_memory(body), "memory change")


@router.get("/hermes-config/files")
async def get_hermes_files(response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return await _run(profile, hermes_files, "files")


@router.get("/hermes-config/viking")
async def get_hermes_viking(response: Response, action: str = "list", uri: Optional[str] = None, q: Optional[str] = None,
                            profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    if action == "forget":
        raise HTTPException(status_code=405, detail="Forgetting is a POST", headers={"Cache-Control": "no-store"})
    job = memory._memory_job(profile, lambda key, _limiter_key: viking(action, {"uri": uri, "q": q}, key))
    return await _run(profile, job, f"OpenViking {action}", memory._memory_executor)


@router.post("/hermes-config/viking/forget")
async def post_hermes_viking_forget(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    body = await core._read_json_body(request, CONFIG_MAX_BODY_BYTES)
    uri = body.get("uri") if isinstance(body, dict) else None
    job = memory._memory_job(profile, lambda key, _limiter_key: viking("forget", {"uri": uri}, key))
    return await _run(profile, job, "OpenViking forget", memory._memory_executor)
