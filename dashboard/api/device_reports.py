"""Conduit's device reports, kept on this host (Asir, 2026-11-15: errors in a me-only app should be logged and worked
on automatically, not reviewed and shared by hand).

Conduit posts each crash, ANR and foreground stall it records (the body it used to send the upstream relay) to
``POST /device-reports``. Reports are grouped by a signature that survives code moving (kind, termination reason,
exception class and the first few Conduit frames, without line numbers) into ``conduit-device-reports/index.json``
under the default Hermes home, with the raw reports beside it. A Hermes cron job watches the index
(``~/.hermes/scripts/conduit_device_reports.py``) and investigates each new or returning problem.

Statuses: ``new`` (not looked at), ``investigating``, ``queued`` (a fix is committed and waits for a build),
``fixed``, ``ignored``. A report for a ``queued`` or ``fixed`` problem from a build newer than the one it was fixed
after reopens it (``generation`` goes up), so a fix that didn't hold comes back.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, Request, Response

from . import core

router = core.router

REPORT_MAX_BYTES = 24_000  # Conduit caps a report at 20,000 bytes
REPORT_LIMIT = 30
REPORT_WINDOW_S = 60.0
KEEP_REPORT_FILES = 300
KEEP_REPORTS_PER_PROBLEM = 5
APP_FRAMES = 3
KINDS = ("crash", "hang")
OPEN = ("new", "investigating")
DONE = ("queued", "fixed")

_limiter = core._MintLimiter(REPORT_LIMIT, REPORT_WINDOW_S, message="Too many device reports; try again shortly")
_FRAME = re.compile(r"^JVM ([A-Za-z_$][A-Za-z0-9_.$<>-]{0,240}):(-?\d+)$")


def reports_root() -> Path:
    """``conduit-device-reports`` under the default Hermes home: the phone's reports aren't any one profile's."""
    try:
        from hermes_constants import get_default_hermes_root
        root = Path(get_default_hermes_root())
    except (ImportError, AttributeError):
        from hermes_constants import get_hermes_home
        root = Path(get_hermes_home())
    return root / "conduit-device-reports"


def _diagnostic(body: Dict[str, Any]) -> Dict[str, Any]:
    diagnostic = body.get("diagnostic")
    if not isinstance(diagnostic, dict):
        return {}
    for key in ("crashDiagnostics", "hangDiagnostics"):
        rows = diagnostic.get(key)
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            return rows[0]
    return {}


def frames(body: Dict[str, Any]) -> List[str]:
    """``class.method`` of each frame, top first, line numbers dropped."""
    stacks = ((_diagnostic(body).get("callStackTree") or {}).get("callStacks") or [])
    out: List[str] = []
    for stack in stacks if isinstance(stacks, list) else []:
        for frame in (stack.get("callStackRootFrames") or []) if isinstance(stack, dict) else []:
            match = _FRAME.match(str((frame or {}).get("binaryName") or "")) if isinstance(frame, dict) else None
            if match:
                out.append(match.group(1))
        if out:
            break
    return out


def signature(body: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """The problem a report belongs to, and what describes it."""
    meta = _diagnostic(body).get("diagnosticMetaData") or {}
    kind = str(body.get("kind") or "")
    reason = str(meta.get("terminationReason") or "")
    exception = str(((meta.get("exceptionReason") or {}) if isinstance(meta.get("exceptionReason"), dict) else {}).get("className") or "")
    every = frames(body)
    ours = [f for f in every if f.startswith("app.hermesconduit.")][:APP_FRAMES] or every[:APP_FRAMES]
    key = "|".join([kind, reason, exception] + ours)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    what = "Crash" if kind == "crash" else "Freeze"
    place = ours[0].rsplit(".", 1)[0].rsplit(".", 1)[-1] + "." + ours[0].rsplit(".", 1)[-1] if ours else "unknown code"
    short = exception.rsplit(".", 1)[-1]
    title = f"{what}: {short} in {place}" if short else f"{what} in {place}"
    return digest, {"kind": kind, "reason": reason, "exception": exception or None, "frames": ours, "title": title}


def _build(body: Dict[str, Any]) -> int:
    try:
        return int(str(body.get("build") or "0"))
    except ValueError:
        return 0


def validate(body: Any) -> Dict[str, Any]:
    if not isinstance(body, dict):
        raise core.TokenError(400, "Send a device report object")
    if body.get("kind") not in KINDS:
        raise core.TokenError(400, "kind is crash or hang")
    if not isinstance(body.get("diagnostic"), dict) or not _diagnostic(body):
        raise core.TokenError(400, "diagnostic is missing")
    for key in ("app_version", "build", "os", "device"):
        if not isinstance(body.get(key), str) or len(body[key]) > 200:
            raise core.TokenError(400, f"{key} is missing")
    return body


class _Locked:
    """The index under an exclusive file lock, written atomically."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def __enter__(self) -> Dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = open(self.root / "index.lock", "a+")
        fcntl.flock(self._lock, fcntl.LOCK_EX)
        try:
            self.index = json.loads((self.root / "index.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.index = {}
        if not isinstance(self.index.get("problems"), dict):
            self.index = {"version": 1, "problems": {}}
        return self.index

    def __exit__(self, kind, value, trace) -> None:
        try:
            if kind is None:
                tmp = self.root / f".index.{os.getpid()}.tmp"
                tmp.write_text(json.dumps(self.index, indent=1, sort_keys=True), encoding="utf-8")
                os.replace(tmp, self.root / "index.json")
        finally:
            fcntl.flock(self._lock, fcntl.LOCK_UN)
            self._lock.close()


def record(body: Dict[str, Any], root: Optional[Path] = None, now: Optional[float] = None) -> Dict[str, Any]:
    """Keeps one report; returns its receipt (``id`` as Conduit checks it) and whether its problem is new."""
    root = root or reports_root()
    now = time.time() if now is None else now
    report_id = f"{int(now * 1000):013d}-{secrets.token_hex(6)}"
    digest, about = signature(body)
    build = _build(body)
    (root / "reports").mkdir(parents=True, exist_ok=True)
    (root / "reports" / f"{report_id}.json").write_text(
        json.dumps({"id": report_id, "received_at": now, "signature": digest, "report": body}, indent=1), encoding="utf-8")
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    with _Locked(root) as index:
        problems = index["problems"]
        problem = problems.get(digest)
        new = problem is None
        if new:
            problem = problems[digest] = {**about, "signature": digest, "status": "new", "generation": 1, "count": 0,
                                          "first_seen": stamp, "builds": [], "reports": [], "note": ""}
        elif problem.get("status") in DONE and build > int(problem.get("fixed_after_build") or 0):
            # A fix that should be in this build didn't hold.
            problem["status"] = "new"
            problem["generation"] = int(problem.get("generation") or 1) + 1
            problem["note"] = f"Came back in build {build} after: {problem.get('note') or 'a fix'}"
            new = True
        problem["count"] = int(problem.get("count") or 0) + 1
        problem["last_seen"] = stamp
        problem["builds"] = sorted({*problem.get("builds", []), build})[-10:]
        problem["reports"] = (problem.get("reports", []) + [report_id])[-KEEP_REPORTS_PER_PROBLEM:]
        problem["app_version"] = body.get("app_version")
    _prune(root / "reports")
    return {"id": report_id, "signature": digest, "new": new, "status": problem["status"]}


def _prune(folder: Path) -> None:
    files = sorted(folder.glob("*.json"))
    for old in files[:-KEEP_REPORT_FILES]:
        try:
            old.unlink()
        except OSError:
            pass


@router.post("/device-reports")
async def post_device_report(request: Request, response: Response, profile: Optional[str] = None) -> Dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    await core._scoped_acquire(_limiter, profile, key="device-reports")
    body = await core._read_json_body(request, REPORT_MAX_BYTES)
    try:
        receipt = record(validate(body))
    except core.TokenError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc), headers={"Cache-Control": "no-store"})
    except Exception as exc:
        raise core._unexpected("device report", exc, feature="Device reports")
    return {"ok": True, **receipt}
