"""Android push registrations (UnifiedPush) for this profile.

Conduit for Android doesn't use the push relay. The phone registers a
UnifiedPush endpoint (an HTTPS URL on ntfy, NextPush, Sunup or a self-hosted
push server) with this profile through the signed-in dashboard connection
(``POST …/push/android/register``), together with its notification
preferences and a fresh end-to-end key; android_push.py then POSTs sealed
pushes straight to that endpoint. Nothing for Android ever goes to the relay.

One record per installation, in the profile's ``conduit-android-push.json``
beside the pairing state, owner-only. The endpoint is a credential (anyone
who has it can push to the phone) and the record holds the end-to-end
secret: neither is ever logged or returned by a status route.

The hooks (agent process) and the dashboard routes (dashboard process) both
use this module: the dashboard loads it by path, so it imports nothing from
the plugin package.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import threading
import time
import urllib.parse
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt
except ImportError:  # everything but Windows
    msvcrt = None  # type: ignore[assignment]

FILE_NAME = "conduit-android-push.json"
VERSION = 1
PLATFORM = "android-unifiedpush"
MAX_REGISTRATIONS = 10
MAX_ENDPOINT_CHARS = 2048
# Set to 1 to accept http:// endpoints (a push server on the LAN). Off by
# default: the endpoint then travels in clear, and it is the only secret.
ALLOW_HTTP_ENV = "CONDUIT_ANDROID_PUSH_ALLOW_HTTP"
# Section 6.5: a registration that hasn't had a success for this long and
# failed at least this many messages is dropped (the phone registers again).
DROP_AFTER_S = 7 * 24 * 3600
DROP_AFTER_FAILURES = 20

# iOS ConduitNotificationPreferences: every flag on except previews.
PREFERENCE_DEFAULTS: dict[str, bool] = {
    "enabled": True,
    "approval_needed": True,
    "input_needed": True,
    "room_invite": True,
    "response_ready": True,
    "turn_failed": True,
    "background_task_finished": True,
    "completion_sound": True,
    "attention_sound": True,
    "show_previews": False,
    "decision_cards": True,
}

_INSTALLATION_ID = re.compile(r"^[0-9a-f-]{1,64}\Z")
_GATEWAY_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}\Z")
_KID = re.compile(r"^[0-9a-f]{32}\Z")
_B64URL = re.compile(r"^[A-Za-z0-9_-]+\Z")
# Cloud instance metadata services: no push server lives there.
_METADATA = {ipaddress.ip_address(a) for a in ("169.254.169.254", "169.254.170.2", "100.100.100.200", "fd00:ec2::254")}
_METADATA_NAMES = ("metadata.google.internal", "metadata", "instance-data", "instance-data.ec2.internal")
_thread_lock = threading.Lock()


class RegistrationError(ValueError):
    """A register or unregister body this host won't accept. The message is
    safe to show (it never echoes the endpoint or the secret)."""


def b64u_decode(value: Any) -> bytes | None:
    if not isinstance(value, str) or not value or not _B64URL.match(value) or len(value) % 4 == 1:
        return None
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError):
        return None


def http_allowed() -> bool:
    return os.environ.get(ALLOW_HTTP_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def check_endpoint(value: Any, *, allow_http: bool | None = None) -> str:
    """The endpoint as given, or RegistrationError. https://, or http:// when
    the operator allows it; no credentials, fragment or metadata host. A query
    is fine (ntfy's ``?up=1``)."""
    if not isinstance(value, str) or not value or len(value) > MAX_ENDPOINT_CHARS \
            or any(not 0x21 <= ord(c) <= 0x7E for c in value) or "\\" in value:
        raise RegistrationError("endpoint must be an https URL of at most 2048 characters")
    try:
        parts = urllib.parse.urlsplit(value)
        host, _ = parts.hostname, parts.port
    except ValueError:
        raise RegistrationError("endpoint must be an https URL") from None
    allow_http = http_allowed() if allow_http is None else allow_http
    if not host or parts.scheme not in (("https", "http") if allow_http else ("https",)):
        raise RegistrationError("endpoint must be an https URL")
    if parts.username is not None or parts.password is not None or parts.fragment or "#" in value:
        raise RegistrationError("endpoint must not contain credentials or a fragment")
    if _is_metadata(host):
        raise RegistrationError("endpoint can't be a cloud metadata address")
    return value


def endpoint_host(endpoint: Any) -> str:
    """The endpoint's host, the only part of it that may be logged."""
    try:
        return urllib.parse.urlsplit(str(endpoint)).hostname or "?"
    except ValueError:
        return "?"


def _is_metadata(host: str) -> bool:
    host = host.lower().rstrip(".")
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return host in _METADATA_NAMES
    mapped = getattr(address, "ipv4_mapped", None)
    return address in _METADATA or (mapped is not None and mapped in _METADATA)


def preferences(raw: Any) -> dict[str, bool]:
    """The iOS preference keys, missing ones at their iOS default."""
    given = raw if isinstance(raw, dict) else {}
    return {key: given[key] if isinstance(given.get(key), bool) else default
            for key, default in PREFERENCE_DEFAULTS.items()}


def _clean(value: Any, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join("".join(c if c.isprintable() else " " for c in value).split())[:maximum]


def validate_register(body: Any, *, allow_http: bool | None = None) -> dict[str, Any]:
    """The registration fields from a register body, or RegistrationError."""
    if not isinstance(body, dict):
        raise RegistrationError("expected a JSON object")
    if body.get("platform") != PLATFORM:
        raise RegistrationError(f"platform must be {PLATFORM}")
    installation_id = body.get("installation_id")
    if not isinstance(installation_id, str) or not _INSTALLATION_ID.match(installation_id):
        raise RegistrationError("installation_id must be 1 to 64 characters of 0-9, a-f and -")
    endpoint = check_endpoint(body.get("endpoint"), allow_http=allow_http)
    if body.get("preferences") is not None and not isinstance(body.get("preferences"), dict):
        raise RegistrationError("preferences must be an object")
    key = body.get("e2e")
    if not isinstance(key, dict):
        raise RegistrationError("e2e is required")
    if key.get("v") != 1 or isinstance(key.get("v"), bool):
        raise RegistrationError("e2e.v must be 1")
    kid = key.get("kid")
    if not isinstance(kid, str) or not _KID.match(kid):
        raise RegistrationError("e2e.kid must be 32 lowercase hex characters")
    secret = key.get("secret")
    decoded = b64u_decode(secret)
    if decoded is None or len(decoded) != 32 or "=" in str(secret):
        raise RegistrationError("e2e.secret must be 32 bytes, base64url without padding")
    record: dict[str, Any] = {
        "installation_id": installation_id,
        "platform": PLATFORM,
        "endpoint": endpoint,
        "kid": kid,
        "secret": secret,
        "preferences": preferences(body.get("preferences")),
        "app_version": _clean(body.get("app_version"), 40),
        "device": _clean(body.get("device"), 120),
    }
    webpush = body.get("webpush")
    if webpush is not None:
        if not isinstance(webpush, dict):
            raise RegistrationError("webpush must be an object")
        # Lenient about padding here: the UnifiedPush connector made these.
        p256dh_text, auth_text = (value.rstrip("=") if isinstance(value, str) else value
                                  for value in (webpush.get("p256dh"), webpush.get("auth")))
        p256dh, auth = b64u_decode(p256dh_text), b64u_decode(auth_text)
        if p256dh is None or len(p256dh) != 65 or p256dh[0] != 0x04:
            raise RegistrationError("webpush.p256dh must be an uncompressed P-256 point, base64url")
        if not _on_curve(p256dh):
            raise RegistrationError("webpush.p256dh is not a P-256 public key")
        if auth is None or len(auth) != 16:
            raise RegistrationError("webpush.auth must be 16 bytes, base64url")
        record["webpush_p256dh"] = p256dh_text
        record["webpush_auth"] = auth_text
    return record


def _on_curve(point: bytes) -> bool:
    try:
        from cryptography.hazmat.primitives.asymmetric import ec
    except Exception:  # noqa: BLE001 — checked again when the push is encrypted
        return True
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)
    except ValueError:
        return False
    return True


def validate_unregister(body: Any) -> str:
    if not isinstance(body, dict):
        raise RegistrationError("expected a JSON object")
    if body.get("platform", PLATFORM) != PLATFORM:
        raise RegistrationError(f"platform must be {PLATFORM}")
    installation_id = body.get("installation_id")
    if not isinstance(installation_id, str) or not _INSTALLATION_ID.match(installation_id):
        raise RegistrationError("installation_id must be 1 to 64 characters of 0-9, a-f and -")
    return installation_id


class AndroidPushStore:
    def __init__(self, home: Path, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(home) / FILE_NAME
        self.clock = clock

    # --- Routes -------------------------------------------------------------

    def register(self, body: Any, *, relay_gateway_id: Any = None, allow_http: bool | None = None) -> dict[str, Any]:
        """Creates or replaces this installation's registration (section 3.1).

        ``relay_gateway_id`` is the profile's relay pairing's gateway id, used
        when it is a valid one; otherwise the profile's own id, made once.
        """
        fields = validate_register(body, allow_http=allow_http)
        now = int(self.clock())
        with self._locked() as state:
            gateway_id = relay_gateway_id if isinstance(relay_gateway_id, str) and _GATEWAY_ID.match(relay_gateway_id) \
                else state["gateway_id"]
            registrations = state["registrations"]
            previous = registrations.get(fields["installation_id"])
            record = {
                **fields,
                "gateway_id": gateway_id,
                "created_at": previous.get("created_at", now) if previous else now,
                "updated_at": now,
                # Kept across re-registration when nothing about the delivery
                # changed; a new endpoint or key starts from scratch.
                "last_success_at": (previous or {}).get("last_success_at")
                if previous and previous.get("endpoint") == fields["endpoint"] else None,
                # Section 3.1 step 4: a registration resets the failure state.
                "failure_count": 0,
                "first_failure_at": None,
            }
            registrations.pop(fields["installation_id"], None)
            registrations[fields["installation_id"]] = record
            while len(registrations) > MAX_REGISTRATIONS:
                oldest = min(registrations, key=lambda key: (registrations[key].get("updated_at") or 0))
                registrations.pop(oldest)
        return {"installation_id": fields["installation_id"], "gateway_id": gateway_id, "kid": fields["kid"]}

    def unregister(self, body: Any) -> bool:
        installation_id = validate_unregister(body)
        with self._locked() as state:
            return state["registrations"].pop(installation_id, None) is not None

    def status(self) -> list[dict[str, Any]]:
        """For diagnostics (GET /e2e): never the endpoint or the secret."""
        if not self.path.exists():
            return []
        return [{
            "installation_id": record["installation_id"],
            "device": record.get("device") or "",
            "app_version": record.get("app_version") or "",
            "kid": record["kid"],
            "last_success_at": record.get("last_success_at"),
            "failing": bool(record.get("failure_count")),
            "webpush": bool(record.get("webpush_p256dh")),
        } for record in self._load()["registrations"].values()]

    # --- Delivery -----------------------------------------------------------

    def registrations(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [dict(record) for record in self._load()["registrations"].values()]

    def has_registrations(self) -> bool:
        return bool(self.registrations())

    def record_success(self, installation_id: str, kid: str, endpoint: str | None = None) -> None:
        with self._locked() as state:
            record = state["registrations"].get(installation_id)
            if record is None or record.get("kid") != kid or (endpoint is not None and record.get("endpoint") != endpoint):
                return
            record["last_success_at"] = int(self.clock())
            record["failure_count"] = 0
            record["first_failure_at"] = None

    def record_failure(self, installation_id: str, kid: str, endpoint: str | None = None) -> bool:
        """Counts one failed message; True when that dropped the registration
        (section 6.5: seven days without a success and 20 failures)."""
        now = int(self.clock())
        with self._locked() as state:
            registrations = state["registrations"]
            record = registrations.get(installation_id)
            if record is None or record.get("kid") != kid or (endpoint is not None and record.get("endpoint") != endpoint):
                return False
            record["failure_count"] = int(record.get("failure_count") or 0) + 1
            if not record.get("first_failure_at"):
                record["first_failure_at"] = now
            since = max(int(record.get("last_success_at") or 0), int(record.get("first_failure_at") or now))
            if record["failure_count"] >= DROP_AFTER_FAILURES and now - since >= DROP_AFTER_S:
                registrations.pop(installation_id)
                return True
            return False

    def remove(self, installation_id: str, kid: str, endpoint: str | None = None) -> bool:
        """Drops the registration the endpoint said is gone (404/410), only if
        it is still the one that was used (not one the phone re-registered)."""
        with self._locked() as state:
            record = state["registrations"].get(installation_id)
            if record is None or record.get("kid") != kid or (endpoint is not None and record.get("endpoint") != endpoint):
                return False
            state["registrations"].pop(installation_id)
            return True

    # --- Storage ------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            raw = None
        return _normalized(raw)

    @contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with _thread_lock, os.fdopen(descriptor, "a+b") as handle, _exclusive(handle):
            state = self._load()
            before = _encoded(state)
            yield state
            after = _encoded(state)
            if after != before or not self.path.exists():
                self._save(after)

    def _save(self, encoded: str) -> None:
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        # Private from the first byte: it holds endpoints and secrets.
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        temporary.replace(self.path)
        self.path.chmod(0o600)


def _normalized(raw: Any) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    gateway_id = raw.get("gateway_id")
    if not isinstance(gateway_id, str) or not _GATEWAY_ID.match(gateway_id):
        gateway_id = str(uuid.uuid4())
    registrations: dict[str, Any] = {}
    stored = raw.get("registrations") if isinstance(raw.get("registrations"), dict) else {}
    for key, record in stored.items():
        if not isinstance(record, dict) or record.get("installation_id") != key:
            continue
        if not isinstance(key, str) or not _INSTALLATION_ID.match(key) or not _KID.match(str(record.get("kid") or "")):
            continue
        if not isinstance(record.get("endpoint"), str) or not isinstance(record.get("gateway_id"), str):
            continue
        registrations[key] = {**record, "preferences": preferences(record.get("preferences"))}
    return {"version": VERSION, "gateway_id": gateway_id, "registrations": registrations}


def _encoded(state: dict[str, Any]) -> str:
    return json.dumps(state, separators=(",", ":")) + "\n"


@contextmanager
def _exclusive(handle: Any) -> Iterator[None]:
    # The same locking as client.state_file_lock.
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    elif msvcrt is not None:
        handle.seek(0)
        for attempt in range(6):  # about a minute
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                break
            except OSError:
                if attempt == 5:
                    raise
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        yield
