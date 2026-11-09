"""Authenticated profile-scoped Android push registration and safe status."""
from __future__ import annotations
import importlib.util
import os
import threading
from typing import Any, Callable, Dict, Optional
from fastapi import HTTPException, Request
from . import core, pairing
logger = core.logger
router = core.router

# --- Android push through UnifiedPush ----------------------------------------
#
# Conduit for Android registers a UnifiedPush endpoint here with its
# notification preferences and a fresh end-to-end key (the same kind of key
# iOS posts to /e2e). The hooks (android_push.py) then POST sealed pushes
# straight to that endpoint: nothing for Android goes to the relay. Stored per
# profile in conduit-android-push.json (android_store.py, loaded by path).
# The app reads a 404 as "this plugin can't push to Android", so these routes
# never answer 404 for a valid request; refusals are {"ok": false, "error"}.

ANDROID_PUSH_LIMIT = 30
ANDROID_PUSH_WINDOW_S = 60.0
ANDROID_PUSH_MAX_BODY_BYTES = 8 * 1024
_android_push_limiter = core._MintLimiter(ANDROID_PUSH_LIMIT, ANDROID_PUSH_WINDOW_S,
                                     message="Too many Android push registrations; try again shortly")
_android_store_module: Any = None
_android_store_lock = threading.Lock()


def _android_store() -> Any:
    """android_store.py beside this folder, loaded once."""
    global _android_store_module
    with _android_store_lock:
        if _android_store_module is None:
            path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "android_store.py")
            spec = importlib.util.spec_from_file_location("conduit_push_dashboard_android_store", path)
            if spec is None or spec.loader is None:
                raise core.TokenError(501, "This plugin install is missing android_store.py; update the plugin")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _android_store_module = module
        return _android_store_module


def _android_home() -> Any:
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def _android_registered() -> bool:
    try:
        return _android_store().AndroidPushStore(_android_home()).has_registrations()
    except Exception:  # noqa: BLE001
        return False


def _with_android_status(status: Dict[str, Any]) -> Dict[str, Any]:
    """GET /e2e plus this profile's Android registrations, when it has any
    (never their endpoints or secrets)."""
    try:
        android = _android_store().AndroidPushStore(_android_home()).status()
    except Exception:  # noqa: BLE001 — diagnostics only
        logger.debug("Conduit couldn't read the Android push registrations", exc_info=True)
        return status
    return {**status, "android": android} if android else status


def register_android_push(body: Any) -> Dict[str, Any]:
    module = _android_store()
    if not pairing._e2e_crypto_available():
        raise core.TokenError(501, "Android push needs the cryptography package on this host")
    state = core._load_pairing_state(core._pairing_state_path())
    try:
        return module.AndroidPushStore(_android_home()).register(
            body, relay_gateway_id=(state or {}).get("gateway_id"))
    except module.RegistrationError as exc:
        raise core.TokenError(400, str(exc))


def unregister_android_push(body: Any) -> Dict[str, Any]:
    module = _android_store()
    try:
        return {"removed": module.AndroidPushStore(_android_home()).unregister(body)}
    except module.RegistrationError as exc:
        raise core.TokenError(400, str(exc))


def _android_refusal(status: int, detail: Any) -> Any:
    from fastapi.responses import JSONResponse

    # A 404 (an unknown profile, say) would read as "no Android push here".
    return JSONResponse(status_code=400 if status == 404 else status,
                        content={"ok": False, "error": str(detail or "request_rejected")},
                        headers={"Cache-Control": "no-store"})


async def _android_route(request: Request, profile: Optional[str], work: Callable[[Any], Dict[str, Any]],
                         route: str) -> Any:
    try:
        await core._scoped_acquire(_android_push_limiter, profile)
        body = await core._read_json_body(request, ANDROID_PUSH_MAX_BODY_BYTES)
        result = await core._run_scoped(profile, lambda: work(body))
    except core.TokenError as exc:
        return _android_refusal(exc.status, str(exc))
    except HTTPException as exc:
        if exc.status_code >= 500:
            raise
        return _android_refusal(exc.status_code, exc.detail)
    except Exception as exc:
        raise core._unexpected(route, exc, feature="Android push")
    from fastapi.responses import JSONResponse

    # The body carried a secret: never cached on the way back either.
    return JSONResponse(content={"ok": True, **result}, headers={"Cache-Control": "no-store"})


@router.post("/push/android/register")
async def post_android_push_register(request: Request, profile: Optional[str] = None) -> Any:
    return await _android_route(request, profile, register_android_push, "register")


@router.post("/push/android/unregister")
async def post_android_push_unregister(request: Request, profile: Optional[str] = None) -> Any:
    return await _android_route(request, profile, unregister_android_push, "unregister")


