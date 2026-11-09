"""Push delivery to Conduit for Android through UnifiedPush.

For every event the plugin turns into an iOS push, each Android registration
of the profile (android_store.py) whose preferences allow it gets the same
end-to-end sealed envelope (e2e.seal_event, with the registration's own
installation id, gateway id and key), POSTed straight to its UnifiedPush
endpoint. The relay is never involved: nothing for Android goes to it.

The body is the iOS push's JSON without ``aps``: a routing stub that says
only the type, and the envelope::

    {"conduit": {"type": "input.needed", "e2e": 1}, "conduit_e2e": {...}}

It must fit 4096 bytes (what every UnifiedPush distributor guarantees):
the body text is shortened, then the approval card dropped, and as a last
resort the phone gets a plain wake-up (``{"conduit": {"type": "wake",
"reason": "too_large"}}``) and finds the event itself when it reconnects.
With the registration's Web Push keys the body is encrypted to the phone
(RFC 8291, webpush.py) before it leaves.

Clarify cards are left out: on iOS they are answered through the relay,
which Android can't reach. The phone answers approvals on its own socket.

Each registration has its own delivery thread, so a slow endpoint holds up
nobody else and one phone's pushes go out one at a time, in order. A failed
POST is retried with backoff (the same bytes: the phone handles each
``kid:msg`` once); an endpoint that answers 404 or 410 is gone and its
registration is dropped.
"""

from __future__ import annotations

import email.utils
import json
import logging
import queue
import random
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from hermes_constants import get_hermes_home

from . import e2e, webpush
from .android_store import AndroidPushStore, b64u_decode, endpoint_host
from .events import PLUGIN_VERSION

logger = logging.getLogger("hermes.plugins.conduit_push")

MAX_BODY_BYTES = 4096
USER_AGENT = f"Hermes-Conduit-Notifier/{PLUGIN_VERSION}"
# Notification types and the preference that gates each (section 4).
NOTIFY_PREFERENCES = {
    "approval.needed": "approval_needed",
    "input.needed": "input_needed",
    "response.ready": "response_ready",
    "turn.failed": "turn_failed",
    "background_task.finished": "background_task_finished",
    "room.invite": "room_invite",
}
# Always sent: the host's call rules and the phone's location settings decide.
UNGATED_TYPES = ("call.requested", "location.requested", "maps.requested")
HIGH_URGENCY = ("approval.needed", "input.needed", "call.requested", "location.requested", "maps.requested")
TTL_S = {"call.requested": 60, "location.requested": 30, "maps.requested": 30}
DEFAULT_TTL_S = 86400
# Section 6.2: the body text is shortened to these lengths in turn.
BODY_STEPS = (400, 120)
BACKOFF_S = (2.0, 4.0, 8.0, 16.0, 32.0)
JITTER = 0.2
MAX_ATTEMPTS = 5
RETRY_AFTER_CAP_S = 600.0
# urllib applies it to the connect and to each read.
CONNECT_TIMEOUT_S = 10.0
QUEUE_SIZE = 64
# An event handed over again (a retried call request) within this long goes
# out once.
RECENT_S = 600.0
MAX_RECENT = 512


def handles(kind: Any) -> bool:
    return kind in NOTIFY_PREFERENCES or kind in UNGATED_TYPES


def allowed(record: dict[str, Any], event: dict[str, Any]) -> bool:
    kind = event.get("type")
    if kind in UNGATED_TYPES:
        return True
    preference = NOTIFY_PREFERENCES.get(kind)
    if preference is None:
        return False
    preferences = record.get("preferences") or {}
    return preferences.get("enabled") is not False and preferences.get(preference) is not False


def ttl_for(kind: Any) -> int:
    return TTL_S.get(kind, DEFAULT_TTL_S)


def urgency_for(kind: Any) -> str:
    return "high" if kind in HIGH_URGENCY else "normal"


def content_for(event: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    """The event as it is sealed for this registration: no clarify card
    (answered through the relay on iOS), and an approval card only when the
    phone wants cards."""
    content = dict(event)
    if event.get("type") == "response.ready" and event.get("level") in ("passive", "active", "time-sensitive"):
        content["interruption_level"] = event["level"]
    decision = content.get("decision")
    if isinstance(decision, dict):
        wants_cards = (record.get("preferences") or {}).get("decision_cards") is not False
        if decision.get("kind") != "approval" or not wants_cards:
            content.pop("decision")
    elif decision is not None:
        content.pop("decision")
    return content


def wake_body(reason: str) -> bytes:
    return json.dumps({"conduit": {"type": "wake", "reason": reason}}, separators=(",", ":")).encode("utf-8")


def _inner(event: dict[str, Any]) -> dict[str, Any]:
    # What e2e.seal_event puts in the envelope.
    return {name: event[name] for name in e2e.INNER_FIELDS if event.get(name) not in (None, "")}


def _ct_chars(inner: dict[str, Any]) -> int:
    packed, _ = e2e._pack(inner)
    sealed = len(packed) + 16  # the Poly1305 tag
    return (4 * sealed + 2) // 3  # unpadded base64url


def _shortened(text: Any, maximum: int) -> Any:
    if not isinstance(text, str) or len(text) <= maximum:
        return text
    return text[:maximum - 1].rstrip() + "…"


def _candidates(content: dict[str, Any]) -> list[dict[str, Any]]:
    """Section 6.2: as is, body at 400 characters, without the card, body at 120."""
    steps = [content, {**content, "body": _shortened(content.get("body"), BODY_STEPS[0])}]
    without_card = {key: value for key, value in steps[-1].items() if key != "decision"}
    steps += [without_card, {**without_card, "body": _shortened(without_card.get("body"), BODY_STEPS[1])}]
    unique: list[dict[str, Any]] = []
    for step in steps:
        if step not in unique:
            unique.append(step)
    return unique


def sealed_body(event: dict[str, Any], record: dict[str, Any], *, now: float | None = None) -> tuple[bytes, bool]:
    """The JSON POSTed to this registration (before Web Push encryption) and
    whether it is the sealed event (False: a plain wake-up)."""
    limit = webpush.MAX_PLAINTEXT if record.get("webpush_p256dh") else MAX_BODY_BYTES
    kind = str(event.get("type") or "")
    try:
        keys = e2e.derive_keys(record["kid"], e2e.unb64u(record["secret"]))
    except Exception as error:  # noqa: BLE001 — no crypto library, or a broken key
        logger.warning("Conduit couldn't seal an Android push (%s); sending a wake-up", error)
        return wake_body("no_key"), False
    for candidate in _candidates(content_for(event, record)):
        # e2e.seal_event would itself drop content to fit an APNs payload:
        # only seal what fits whole, so the order above is the one applied.
        if _ct_chars(_inner(candidate)) > e2e.MAX_CT_CHARS:
            continue
        try:
            sealed = e2e.seal_event(candidate, keys, installation_id=record["installation_id"],
                                    gateway_id=record["gateway_id"], now=now)
        except Exception as error:  # noqa: BLE001
            logger.warning("Conduit couldn't seal an Android push (%s); sending a wake-up", error)
            return wake_body("seal_failed"), False
        body = json.dumps({"conduit": {"type": kind, "e2e": 1}, "conduit_e2e": sealed["e2e"]},
                          separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(body) <= limit:
            return body, True
    return wake_body("too_large"), False


def encoded(body: bytes, record: dict[str, Any]) -> tuple[bytes, dict[str, str]]:
    """The bytes on the wire and their content headers: Web Push encrypted
    when the registration has keys."""
    p256dh, auth = b64u_decode(record.get("webpush_p256dh")), b64u_decode(record.get("webpush_auth"))
    if p256dh is not None and auth is not None:
        return (webpush.encrypt(body, p256dh, auth),
                {"Content-Type": "application/octet-stream", "Content-Encoding": "aes128gcm"})
    return body, {"Content-Type": "application/json"}


@dataclass
class Job:
    home: str
    installation_id: str
    kid: str
    endpoint: str
    kind: str
    data: bytes
    headers: dict[str, str]
    deadline: float
    # The plain wake-up sent once instead when the endpoint says 413.
    fallback: "Job | None" = None
    attempts: int = field(default=0)


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_opener = urllib.request.build_opener(_RefuseRedirects)
# A LAN push server over plain http:// never goes through a proxy.
_direct_opener = urllib.request.build_opener(_RefuseRedirects, urllib.request.ProxyHandler({}))


def http_post(url: str, data: bytes, headers: dict[str, str], timeout: float) -> tuple[int, dict[str, str]]:
    """POST without cookies, credentials or redirects; (status, headers).
    Connection errors and timeouts raise."""
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    opener = _opener if url.lower().startswith("https://") else _direct_opener
    try:
        with opener.open(request, timeout=timeout) as response:
            response.read(4096)
            return response.status, {key.lower(): value for key, value in response.headers.items()}
    except urllib.error.HTTPError as error:
        return error.code, {key.lower(): value for key, value in (error.headers or {}).items()}


def retry_after(value: Any, now: float) -> float | None:
    """Seconds from a Retry-After header (seconds or an HTTP date)."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.isdigit():
        return float(text)
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    return max(0.0, when.timestamp() - now)


class Deliverer:
    """One delivery thread per registration; ``threaded=False`` delivers
    inline (tests)."""

    def __init__(self, *, post: Callable[..., tuple[int, dict[str, str]]] = http_post,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
                 jitter: Callable[[], float] = random.random, threaded: bool = True) -> None:
        self.post = post
        self.sleep = sleep
        self.clock = clock
        self.jitter = jitter
        self.threaded = threaded
        self._lock = threading.Lock()
        self._queues: dict[tuple[str, str], queue.Queue[Job]] = {}
        self._recent: dict[tuple[str, str, str, str], float] = {}
        self._stop = threading.Event()

    def start(self) -> None:
        with self._lock:
            if self._stop.is_set():
                self._stop = threading.Event()

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            self._queues.clear()
            self._recent.clear()

    # --- Handing events over ----------------------------------------------

    def dispatch(self, event: dict[str, Any], home: Path | None = None) -> int:
        """Queues ``event`` for each Android registration that wants it; the
        number of registrations it goes (or already went) to."""
        if self._stop.is_set() or not isinstance(event, dict) or not handles(event.get("type")):
            return 0
        home = Path(home) if home is not None else get_hermes_home()
        store = AndroidPushStore(home, clock=self.clock)
        records = store.registrations()
        count = 0
        for record in records:
            if not allowed(record, event):
                continue
            seen = (str(home), record["installation_id"], record["kid"], str(event.get("event_id") or ""))
            if seen[3] and self._seen(seen):
                count += 1
                continue
            try:
                job = self._job(event, record, str(home))
            except Exception:  # noqa: BLE001 — never break the hook
                logger.warning("Conduit couldn't build an Android push for %s", endpoint_host(record.get("endpoint")),
                               exc_info=True)
                continue
            if self._enqueue(job):
                count += 1
                if seen[3]:
                    self._remember(seen)
        return count

    def _job(self, event: dict[str, Any], record: dict[str, Any], home: str) -> Job:
        kind = str(event.get("type") or "")
        now = self.clock()
        headers = {"TTL": str(ttl_for(kind)), "Urgency": urgency_for(kind), "User-Agent": USER_AGENT}
        body, _ = sealed_body(event, record, now=now)
        data, content = encoded(body, record)
        wake_data, wake_content = encoded(wake_body("too_large"), record)
        common = dict(home=home, installation_id=record["installation_id"], kid=record["kid"],
                      endpoint=record["endpoint"], kind=kind, deadline=now + ttl_for(kind))
        fallback = Job(**common, data=wake_data, headers={**headers, **wake_content})
        return Job(**common, data=data, headers={**headers, **content}, fallback=fallback)

    def _seen(self, key: tuple[str, str, str, str]) -> bool:
        now = self.clock()
        with self._lock:
            when = self._recent.get(key)
            return when is not None and now - when < RECENT_S

    def _remember(self, key: tuple[str, str, str, str]) -> None:
        now = self.clock()
        with self._lock:
            self._recent.pop(key, None)
            self._recent[key] = now
            while len(self._recent) > MAX_RECENT:
                self._recent.pop(next(iter(self._recent)))

    def _enqueue(self, job: Job) -> bool:
        if self._stop.is_set():
            return False
        if not self.threaded:
            self.deliver(job)
            return True
        key = (job.home, job.installation_id)
        with self._lock:
            if self._stop.is_set():
                return False
            pending = self._queues.get(key)
            if pending is None:
                pending = self._queues[key] = queue.Queue(maxsize=QUEUE_SIZE)
                threading.Thread(target=self._run, args=(key, pending, self._stop), name="conduit-android-push", daemon=True).start()
            try:
                pending.put_nowait(job)
            except queue.Full:
                logger.warning("Conduit Android push queue for %s is full; dropping %s", endpoint_host(job.endpoint), job.kind)
                return False
        return True

    def _run(self, key: tuple[str, str], pending: "queue.Queue[Job]", stopped: threading.Event) -> None:
        idle_since = time.monotonic()
        try:
            while not stopped.is_set():
                try:
                    job = pending.get(timeout=1.0)
                except queue.Empty:
                    if time.monotonic() - idle_since >= 30:
                        # Retire only while holding the enqueue lock, so a
                        # new event cannot land in an abandoned queue.
                        with self._lock:
                            if pending.empty() and self._queues.get(key) is pending:
                                del self._queues[key]
                                return
                    continue
                try:
                    self.deliver(job, stopped=stopped)
                except Exception:
                    logger.warning("Conduit Android push delivery failed", exc_info=True)
                finally:
                    pending.task_done()
                    idle_since = time.monotonic()
        finally:
            with self._lock:
                if self._queues.get(key) is pending:
                    del self._queues[key]

    # --- Delivering -------------------------------------------------------

    def deliver(self, job: Job, *, stopped: threading.Event | None = None) -> str:
        """Sends one message (section 6.5): ``sent``, ``gone``, ``failed``
        or, after a 413, the outcome of the wake-up sent instead."""
        store = AndroidPushStore(Path(job.home), clock=self.clock)
        host = endpoint_host(job.endpoint)
        stopped = stopped or self._stop
        while True:
            if stopped.is_set() or self.clock() >= job.deadline:
                return "cancelled"
            current = next((r for r in store.registrations() if r["installation_id"] == job.installation_id), None)
            if current is None or current.get("kid") != job.kid or current.get("endpoint") != job.endpoint or not allowed(current, {"type": job.kind}):
                return "cancelled"
            job.attempts += 1
            try:
                status, headers = self.post(job.endpoint, job.data, dict(job.headers), CONNECT_TIMEOUT_S)
            except Exception as error:  # noqa: BLE001 — connection error or timeout
                status, headers = 0, {}
                logger.info("Conduit Android push to %s failed: %s", host, type(error).__name__)
            if 200 <= status < 300:
                store.record_success(job.installation_id, job.kid, job.endpoint)
                return "sent"
            if status in (404, 410):
                if store.remove(job.installation_id, job.kid, job.endpoint):
                    logger.warning("Conduit dropped an Android push registration: %s says it is gone (%s)", host, status)
                return "gone"
            if status == 413 and job.fallback is not None:
                logger.warning("Conduit Android push to %s was too large; sending a wake-up instead", host)
                return self.deliver(replace(job.fallback, fallback=None, attempts=0), stopped=stopped)
            if status == 429:
                delay = retry_after(headers.get("retry-after"), self.clock())
                delay = min(RETRY_AFTER_CAP_S, delay) if delay is not None else self._backoff(job.attempts)
            elif status == 0 or status >= 500:
                delay = self._backoff(job.attempts)
            else:
                # Any other answer (a 4xx, a refused redirect): this message
                # won't go, and sending it again won't change that.
                delay = None
            if delay is None or job.attempts >= MAX_ATTEMPTS or self.clock() + delay > job.deadline:
                if store.record_failure(job.installation_id, job.kid, job.endpoint):
                    logger.warning("Conduit dropped an Android push registration at %s after a week of failures", host)
                logger.warning("Conduit Android push to %s failed (%s) after %d attempt(s)", host, status or "no answer",
                               job.attempts)
                return "failed"
            if self.threaded:
                if stopped.wait(delay):
                    return "cancelled"
            else:
                self.sleep(delay)

    def _backoff(self, attempt: int) -> float:
        base = BACKOFF_S[min(attempt, len(BACKOFF_S)) - 1]
        return base * (1.0 + JITTER * (2.0 * self.jitter() - 1.0))


deliverer = Deliverer()


def dispatch(event: dict[str, Any], home: Path | None = None) -> int:
    return deliverer.dispatch(event, home)


def registered(home: Path | None = None) -> bool:
    try:
        return AndroidPushStore(Path(home) if home is not None else get_hermes_home()).has_registrations()
    except Exception:  # noqa: BLE001
        return False
