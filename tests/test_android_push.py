"""Android push through UnifiedPush (Conduit for Android, docs/ANDROID_PUSH.md
in the app's repository): the register/unregister routes, the store, and
delivery of the same E2E-sealed envelope iOS gets, POSTed to the phone's
endpoint instead of the relay."""

import base64
import contextlib
import http.server
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import threading
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"

if "hermes_constants" not in sys.modules:
    _hermes_constants = types.ModuleType("hermes_constants")
    _hermes_constants.get_hermes_home = lambda: pathlib.Path(tempfile.gettempdir())
    sys.modules["hermes_constants"] = _hermes_constants
if "conduit_push" not in sys.modules:
    _pkg = types.ModuleType("conduit_push")
    _pkg.__path__ = [str(ROOT)]
    sys.modules["conduit_push"] = _pkg


def _load(name, path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


e2e = _load("conduit_push.e2e", ROOT / "e2e.py")
events = _load("conduit_push.events", ROOT / "events.py")
client = _load("conduit_push.client", ROOT / "client.py")
store_module = _load("conduit_push.android_store", ROOT / "android_store.py")
webpush = _load("conduit_push.webpush", ROOT / "webpush.py")
android_push = _load("conduit_push.android_push", ROOT / "android_push.py")
calls = _load("conduit_push.calls", ROOT / "calls.py")
api = _load("conduit_plugin_api_android_push", ROOT / "dashboard" / "plugin_api.py")

AndroidPushStore = store_module.AndroidPushStore

# The vector docs/ANDROID_PUSH.md section 5, PushE2ETest.kt and
# NotificationE2ETests.swift share.
KID = "0123456789abcdef0123456789abcdef"
SECRET = bytes(range(32))
INSTALLATION = "11111111-2222-3333-4444-555555555555"
GATEWAY = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
ENDPOINT = "https://ntfy.example/upAbCdEf0123456789?up=1"
SPEC_ENVELOPE = {
    "v": 1,
    "kid": KID,
    "msg": "input:9ab3405eb5333adb44e353fcd81508e8",
    "iat": 1760000000,
    "tok": "5d671a3554c84fb7",
    "z": 1,
    "req": "conduit-push-abc123def456",
    "n": "BwcHBwcHBwcHBwcH",
    "ct": "YEtXcEZp_hUYxXaIBmF-kC5gT4i7IomTbFRQmwF8LiFNeAzHOrZSd-VdKXI8WBkdjNd9GCKsCqePYurZuUJCINjnT2wiVZAGyBa2rq2UZfKo9399"
          "Ql8p-f4Rywl3wgNKh33TNk8pGP0EvdOuI6zi3Haw-c1-bCrheVS1BkufqRdPe2WPDPElQCiw0RU6PRCeEwLbFd7G30C0RKQDmExz-ZyYrQ",
}
NOW = 1_760_000_000.0


def _phone_key():
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    return ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(Encoding.X962,
                                                                              PublicFormat.UncompressedPoint)


VALID_P256DH = base64.urlsafe_b64encode(_phone_key()).rstrip(b"=").decode()


def _b64u(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _body(installation=INSTALLATION, endpoint=ENDPOINT, kid=KID, secret=SECRET, **extra):
    body = {
        "installation_id": installation,
        "endpoint": endpoint,
        "platform": "android-unifiedpush",
        "preferences": {"enabled": True, "approval_needed": True, "input_needed": True, "response_ready": True,
                        "turn_failed": True, "background_task_finished": True, "completion_sound": False,
                        "attention_sound": True, "show_previews": True, "decision_cards": True},
        "e2e": {"v": 1, "kid": kid, "secret": _b64u(secret)},
        "app_version": "0.1.0",
        "device": "Google Pixel 8",
    }
    body.update(extra)
    return body


def _open(body, record=None):
    """What the phone does: open the envelope with its key, bound to the type
    in the routing stub and the gateway id register returned."""
    message = json.loads(body)
    assert set(message) == {"conduit", "conduit_e2e"}
    stub = message["conduit"]
    # The routing stub says the type and nothing else.
    assert set(stub) == {"type", "e2e"} and stub["e2e"] == 1
    record = record or {}
    keys = e2e.derive_keys(record.get("kid", KID), SECRET)
    return e2e.open_event({"type": stub["type"], "e2e": message["conduit_e2e"]}, keys,
                          installation_id=record.get("installation_id", INSTALLATION),
                          gateway_id=record.get("gateway_id", GATEWAY)), message["conduit_e2e"]


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    slept: list


class Endpoint:
    """A fake push server: answers in turn, then 201."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.posts = []

    def __call__(self, url, data, headers, timeout):
        self.posts.append({"url": url, "data": data, "headers": headers, "timeout": timeout})
        if self.answers:
            answer = self.answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer if isinstance(answer, tuple) else (answer, {})
        return 201, {}


@pytest.fixture()
def clock():
    value = Clock()
    value.slept = []
    return value


@pytest.fixture()
def home(tmp_path):
    return tmp_path


def _register(home, clock, body=None, gateway=GATEWAY):
    return AndroidPushStore(home, clock=clock).register(body or _body(), relay_gateway_id=gateway)


def _deliverer(clock, endpoint):
    return android_push.Deliverer(post=endpoint, sleep=clock.sleep, clock=clock, jitter=lambda: 0.5, threaded=False)


def _event(kind="response.ready", **extra):
    event = events.push_event(kind, identifier=f"{kind.split('.')[0]}:0123456789abcdef", session_id="sess-1",
                              profile="coder", title="Hermes", body="Deploy to prod?")
    event.update(extra)
    return event


# --- Compatibility with the app's vector -------------------------------------


def test_the_push_key_is_the_one_the_app_derives():
    assert e2e.derive_keys(KID, SECRET).push.hex() == "a5a8884943a780bbf4f846cf294f149d8c668f58312ca26841ec7f78e5a1a099"


def test_the_spec_envelope_opens_with_the_plugins_own_code():
    # The Android phone opens exactly what the iOS phone opens: same AAD,
    # same compression, same fields.
    content = e2e.open_event({"type": "input.needed", "e2e": SPEC_ENVELOPE}, e2e.derive_keys(KID, SECRET),
                             installation_id=INSTALLATION, gateway_id=GATEWAY)
    assert content["title"] == "Hermes" and content["body"] == "Deploy to prod?"
    assert content["session_id"] == "sess-1" and content["profile"] == "coder"
    assert content["decision"]["kind"] == "clarify" and content["decision"]["request_id"] == SPEC_ENVELOPE["req"]
    with pytest.raises(e2e.E2EError):
        e2e.open_event({"type": "input.needed", "e2e": SPEC_ENVELOPE}, e2e.derive_keys(KID, SECRET),
                       installation_id=INSTALLATION, gateway_id="another-gateway")


def test_web_push_encryption_matches_rfc_8291():
    # RFC 8291 section 5.
    from cryptography.hazmat.primitives.asymmetric import ec

    def d(text):
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))

    sender = ec.derive_private_key(int.from_bytes(d("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"), ec.SECP256R1())
    receiver = ec.derive_private_key(int.from_bytes(d("q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94"), "big"),
                                     ec.SECP256R1())
    body = webpush.encrypt(b"When I grow up, I want to be a watermelon",
                           d("BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"),
                           d("BTBZMqHH6r4Tts7J_aSIgg"), salt=d("DGv6ra1nlYgDCS1FRnbzlw"), private_key=sender)
    assert _b64u(body) == (
        "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3"
        "jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN")
    assert webpush.decrypt(body, receiver, d("BTBZMqHH6r4Tts7J_aSIgg")) == b"When I grow up, I want to be a watermelon"
    assert webpush.MAX_PLAINTEXT == 3993


# --- Routes ------------------------------------------------------------------


@pytest.fixture()
def dashboard(tmp_path, monkeypatch):
    monkeypatch.setattr(api.android_push, "_android_home", lambda: tmp_path)
    monkeypatch.setattr(api.core, "_pairing_state_path", lambda: tmp_path / "conduit-push.json")
    monkeypatch.setattr(api.core, "_profile_scope", lambda profile: contextlib.nullcontext())
    monkeypatch.delenv(store_module.ALLOW_HTTP_ENV, raising=False)
    api.android_push._android_push_limiter._mints.clear()
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def test_register_answers_the_gateway_id_and_echoes_the_key(dashboard, tmp_path):
    response = dashboard.post(f"{BASE}/push/android/register?profile=coder", json=_body())
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    answer = response.json()
    assert answer["ok"] is True and answer["installation_id"] == INSTALLATION and answer["kid"] == KID
    gateway = answer["gateway_id"]
    assert 1 <= len(gateway) <= 64 and set(gateway) <= set("0123456789abcdefABCDEF-_" + "ghijklmnopqrstuvwxyz")
    # Idempotent, and the profile's own id is kept.
    assert dashboard.post(f"{BASE}/push/android/register?profile=coder", json=_body()).json() == answer
    path = tmp_path / store_module.FILE_NAME
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    stored = json.loads(path.read_text())["registrations"][INSTALLATION]
    assert stored["endpoint"] == ENDPOINT and stored["gateway_id"] == gateway and stored["kid"] == KID


def test_register_uses_the_relay_gateway_id_when_the_profile_is_paired(dashboard, tmp_path):
    (tmp_path / "conduit-push.json").write_text(json.dumps({"credential": "c", "installation_id": "ios",
                                                            "gateway_id": "relay-gw_1"}))
    assert dashboard.post(f"{BASE}/push/android/register", json=_body()).json()["gateway_id"] == "relay-gw_1"


@pytest.mark.parametrize("change, reason", [
    ({"platform": "fcm"}, "platform"),
    ({"installation_id": "NOT-HEX"}, "installation_id"),
    ({"endpoint": "http://ntfy.example/up"}, "https"),
    ({"endpoint": "https://user:pw@ntfy.example/up"}, "credentials"),
    ({"endpoint": "https://169.254.169.254/up"}, "metadata"),
    ({"endpoint": "https://ntfy.example/" + "a" * 2048}, "2048"),
    ({"e2e": {"v": 1, "kid": "XYZ", "secret": _b64u(SECRET)}}, "kid"),
    ({"e2e": {"v": 1, "kid": KID, "secret": _b64u(b"short")}}, "secret"),
    ({"e2e": {"v": 2, "kid": KID, "secret": _b64u(SECRET)}}, "e2e.v"),
    ({"webpush": {"p256dh": _b64u(b"\x04" + b"x" * 10), "auth": _b64u(b"a" * 16)}}, "p256dh"),
    ({"webpush": {"p256dh": _b64u(b"\x04" + b"x" * 64), "auth": _b64u(b"a" * 16)}}, "P-256"),
    ({"webpush": {"p256dh": VALID_P256DH, "auth": _b64u(b"a" * 8)}}, "auth"),
])
def test_register_refuses_a_bad_body_with_400_never_404(dashboard, change, reason):
    response = dashboard.post(f"{BASE}/push/android/register", json=_body(**change))
    assert response.status_code == 400
    assert response.json()["ok"] is False and reason in response.json()["error"]
    # Nothing in the refusal echoes the endpoint or the secret.
    assert _b64u(SECRET) not in response.text and "ntfy.example" not in response.text


def test_register_refuses_a_body_that_isnt_json_with_400(dashboard):
    response = dashboard.post(f"{BASE}/push/android/register", content=b"{nope",
                              headers={"Content-Type": "application/json"})
    assert response.status_code == 400 and response.json()["ok"] is False


def test_an_unknown_profile_is_a_400_not_a_404(dashboard, monkeypatch):
    def unknown(profile):
        raise api.android_push.HTTPException(status_code=404, detail="Profile not found")

    monkeypatch.setattr(api.core, "_profile_scope", unknown)
    response = dashboard.post(f"{BASE}/push/android/register?profile=nope", json=_body())
    assert response.status_code == 400 and response.json() == {"ok": False, "error": "Profile not found"}


def test_http_endpoints_need_the_operator_switch(dashboard, monkeypatch):
    monkeypatch.setenv(store_module.ALLOW_HTTP_ENV, "1")
    response = dashboard.post(f"{BASE}/push/android/register", json=_body(endpoint="http://192.168.1.5:8080/up1"))
    assert response.status_code == 200


def test_a_new_key_replaces_the_old_one_and_resets_failures(home, clock):
    _register(home, clock)
    store = AndroidPushStore(home, clock=clock)
    store.record_failure(INSTALLATION, KID)
    other = "f" * 32
    _register(home, clock, _body(kid=other))
    [record] = store.registrations()
    assert record["kid"] == other and record["failure_count"] == 0
    # Results for the old key don't touch the new registration.
    assert store.remove(INSTALLATION, KID) is False
    assert store.registrations()


def test_the_eleventh_installation_drops_the_least_recently_updated(home, clock):
    for index in range(11):
        clock.now += 1
        _register(home, clock, _body(installation=f"{index:08x}"))
    ids = {record["installation_id"] for record in AndroidPushStore(home).registrations()}
    assert len(ids) == 10 and "00000000" not in ids and "0000000a" in ids


def test_missing_preferences_default_as_on_ios(home, clock):
    body = _body()
    body["preferences"] = {"response_ready": False}
    _register(home, clock, body)
    preferences = AndroidPushStore(home).registrations()[0]["preferences"]
    assert preferences["response_ready"] is False and preferences["show_previews"] is False
    assert all(preferences[key] for key in preferences if key not in ("response_ready", "show_previews"))


def test_unregister_is_idempotent(dashboard):
    dashboard.post(f"{BASE}/push/android/register", json=_body())
    body = {"installation_id": INSTALLATION, "platform": "android-unifiedpush"}
    assert dashboard.post(f"{BASE}/push/android/unregister", json=body).json() == {"ok": True, "removed": True}
    assert dashboard.post(f"{BASE}/push/android/unregister", json=body).json() == {"ok": True, "removed": False}
    assert dashboard.post(f"{BASE}/push/android/unregister", json={**body, "platform": "x"}).status_code == 400


def test_e2e_status_lists_android_phones_without_secrets(dashboard):
    before = dashboard.get(f"{BASE}/e2e").json()
    assert "android" not in before  # unchanged for iOS-only profiles
    dashboard.post(f"{BASE}/push/android/register", json=_body())
    status = dashboard.get(f"{BASE}/e2e").json()
    [phone] = status["android"]
    assert phone["installation_id"] == INSTALLATION and phone["device"] == "Google Pixel 8" and phone["kid"] == KID
    assert phone["failing"] is False and phone["last_success_at"] is None
    assert _b64u(SECRET) not in json.dumps(status) and "ntfy" not in json.dumps(status)


def test_capabilities_name_android_push(dashboard):
    assert "android-push" in dashboard.get(f"{BASE}/capabilities").json()["capabilities"]


# --- What goes out -----------------------------------------------------------


def test_a_reply_goes_to_the_endpoint_sealed_with_the_phones_key(home, clock):
    _register(home, clock)
    endpoint = Endpoint()
    assert _deliverer(clock, endpoint).dispatch(_event(), home) == 1
    [post] = endpoint.posts
    assert post["url"] == ENDPOINT
    assert post["headers"]["Content-Type"] == "application/json"
    assert post["headers"]["TTL"] == "86400" and post["headers"]["Urgency"] == "normal"
    assert "Authorization" not in post["headers"] and "Cookie" not in post["headers"]
    assert len(post["data"]) <= 4096
    for text in (b"Deploy to prod?", b"sess-1", b"coder", b"Hermes"):
        assert text not in post["data"]
    content, envelope = _open(post["data"])
    assert content == {"title": "Hermes", "body": "Deploy to prod?", "session_id": "sess-1", "profile": "coder"}
    assert envelope["iat"] == int(NOW) and envelope["req"] == "" and len(envelope["tok"]) == 16
    assert AndroidPushStore(home).registrations()[0]["last_success_at"] == int(NOW)


def test_the_envelope_is_the_one_ios_gets_for_the_same_event(home, clock):
    # Same seal_event, so the fields the phone checks are built the same way.
    _register(home, clock)
    endpoint = Endpoint()
    event = _event()
    _deliverer(clock, endpoint).dispatch(event, home)
    _, envelope = _open(endpoint.posts[0]["data"])
    ios = e2e.seal_event(event, e2e.derive_keys(KID, SECRET), installation_id=INSTALLATION, gateway_id=GATEWAY, now=NOW)
    assert {key: envelope[key] for key in ("v", "kid", "msg", "iat", "tok", "z", "req")} == \
           {key: ios["e2e"][key] for key in ("v", "kid", "msg", "iat", "tok", "z", "req")}


def test_preferences_gate_notifications_but_not_calls_or_locates(home, clock):
    body = _body()
    body["preferences"] = {"enabled": False}
    _register(home, clock, body)
    endpoint = Endpoint()
    deliverer = _deliverer(clock, endpoint)
    for kind in ("approval.needed", "input.needed", "response.ready", "turn.failed", "background_task.finished"):
        assert deliverer.dispatch(_event(kind), home) == 0
    call = calls.call_event({"id": "watch0123456789", "title": "Deploy", "session_ids": ["sess-1"]}, "done",
                            session_id="sess-1", profile="coder")
    assert deliverer.dispatch(call, home) == 1
    locate = events.push_event("location.requested", identifier="locate:l_0123456789abcdef", profile="coder",
                               locate={"id": "l_0123456789abcdef"})
    assert deliverer.dispatch(locate, home) == 1
    ring, where = endpoint.posts
    assert ring["headers"]["TTL"] == "60" and ring["headers"]["Urgency"] == "high"
    assert where["headers"]["TTL"] == "30" and where["headers"]["Urgency"] == "high"
    assert _open(ring["data"])[0]["call"]["id"] == "watch0123456789"
    content, _ = _open(where["data"])
    assert content == {"profile": "coder", "locate": {"id": "l_0123456789abcdef"}}


def test_one_preference_turns_one_type_off(home, clock):
    body = _body()
    body["preferences"] = {"response_ready": False}
    _register(home, clock, body)
    deliverer = _deliverer(clock, Endpoint())
    assert deliverer.dispatch(_event("response.ready"), home) == 0
    assert deliverer.dispatch(_event("turn.failed"), home) == 1


def test_other_events_never_go_to_android(home, clock):
    _register(home, clock)
    endpoint = Endpoint()
    deliverer = _deliverer(clock, endpoint)
    assert deliverer.dispatch(events.plugin_hello(), home) == 0
    assert deliverer.dispatch(events.push_event("phone.changes", identifier="phone:1", profile="coder"), home) == 0
    assert endpoint.posts == []


def test_clarify_cards_are_left_out_for_android(home, clock):
    _register(home, clock)
    endpoint = Endpoint()
    decision = events.clarify_decision(request_id="conduit-push-abc123def456", question="Deploy to prod?",
                                       choices=["Yes", "No"])
    _deliverer(clock, endpoint).dispatch(_event("input.needed", decision=decision), home)
    content, envelope = _open(endpoint.posts[0]["data"])
    assert "decision" not in content and envelope["req"] == ""
    assert content["body"] == "Deploy to prod?"


def test_approval_cards_follow_the_decision_cards_preference(home, clock):
    _register(home, clock)
    decision = events.approval_decision(session_key="sess-1", description="rm -rf build")
    endpoint = Endpoint()
    _deliverer(clock, endpoint).dispatch(_event("approval.needed", decision=decision), home)
    content, _ = _open(endpoint.posts[0]["data"])
    assert content["decision"] == decision
    assert decision["kind"] == "approval" and decision["session_key"] == "sess-1"
    body = _body()
    body["preferences"] = {"decision_cards": False}
    _register(home, clock, body)
    endpoint = Endpoint()
    _deliverer(clock, endpoint).dispatch(_event("approval.needed", decision=decision, event_id="approval:2"), home)
    assert "decision" not in _open(endpoint.posts[0]["data"])[0]


def test_a_long_body_is_shortened_to_fit_4096_bytes(home, clock):
    _register(home, clock)
    endpoint = Endpoint()
    noise = base64.b64encode(os.urandom(2400)).decode()  # 3200 characters that don't compress
    _deliverer(clock, endpoint).dispatch({**_event(), "body": noise}, home)
    data = endpoint.posts[0]["data"]
    assert len(data) <= 4096
    content, _ = _open(data)
    assert len(content["body"]) <= 400 and content["body"].endswith("…") and content["title"] == "Hermes"


def test_the_card_goes_before_the_body_is_cut_to_120(home, clock):
    _register(home, clock)
    endpoint = Endpoint()
    noise = base64.b64encode(os.urandom(2100)).decode()  # doesn't fit beside the rest
    decision = {"kind": "approval", "session_key": "sess-1", "description": noise, "choices": ["once", "deny"]}
    _deliverer(clock, endpoint).dispatch({**_event("approval.needed"), "decision": decision}, home)
    content, _ = _open(endpoint.posts[0]["data"])
    assert "decision" not in content and content["body"] == "Deploy to prod?"


def test_what_never_fits_becomes_a_plain_wake_up(home, clock):
    _register(home, clock)
    endpoint = Endpoint()
    noise = base64.b64encode(os.urandom(3000)).decode()
    _deliverer(clock, endpoint).dispatch({**_event(), "title": noise}, home)
    assert json.loads(endpoint.posts[0]["data"]) == {"conduit": {"type": "wake", "reason": "too_large"}}


def test_web_push_keys_encrypt_the_body(home, clock):
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    phone = ec.generate_private_key(ec.SECP256R1())
    p256dh = phone.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    auth = os.urandom(16)
    _register(home, clock, _body(webpush={"p256dh": _b64u(p256dh), "auth": _b64u(auth)}))
    endpoint = Endpoint()
    noise = base64.b64encode(os.urandom(2400)).decode()
    deliverer = _deliverer(clock, endpoint)
    deliverer.dispatch(_event(), home)
    deliverer.dispatch({**_event(), "event_id": "response:other", "body": noise}, home)
    for post in endpoint.posts:
        assert post["headers"]["Content-Type"] == "application/octet-stream"
        assert post["headers"]["Content-Encoding"] == "aes128gcm"
        assert len(post["data"]) <= 4096 and b"conduit" not in post["data"]
    content, _ = _open(webpush.decrypt(endpoint.posts[0]["data"], phone, auth))
    assert content["body"] == "Deploy to prod?"
    assert len(_open(webpush.decrypt(endpoint.posts[1]["data"], phone, auth))[0]["body"]) <= 400


def test_padded_web_push_keys_are_accepted(home, clock):
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    point = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(Encoding.X962,
                                                                               PublicFormat.UncompressedPoint)
    p256dh = base64.urlsafe_b64encode(point).decode()
    auth = base64.urlsafe_b64encode(bytes(16)).decode()
    assert p256dh.endswith("=") and auth.endswith("==")
    _register(home, clock, _body(webpush={"p256dh": p256dh, "auth": auth}))
    record = AndroidPushStore(home).registrations()[0]
    assert record["webpush_p256dh"] == p256dh.rstrip("=") and record["webpush_auth"] == auth.rstrip("=")


def test_the_same_event_goes_out_once(home, clock):
    # calls.deliver retries the relay with the same event.
    _register(home, clock)
    endpoint = Endpoint()
    deliverer = _deliverer(clock, endpoint)
    assert deliverer.dispatch(_event(), home) == 1
    assert deliverer.dispatch(_event(), home) == 1
    assert len(endpoint.posts) == 1


# --- Retries and dropping ----------------------------------------------------


@pytest.mark.parametrize("status", [404, 410])
def test_a_gone_endpoint_drops_the_registration_at_once(home, clock, status):
    _register(home, clock)
    endpoint = Endpoint(status)
    _deliverer(clock, endpoint).dispatch(_event(), home)
    assert len(endpoint.posts) == 1 and AndroidPushStore(home).registrations() == []


def test_server_errors_retry_with_backoff_and_the_same_bytes(home, clock):
    _register(home, clock)
    endpoint = Endpoint(500, OSError("reset"), 503, 201)
    _deliverer(clock, endpoint).dispatch(_event(), home)
    assert clock.slept == [2.0, 4.0, 8.0]
    assert len({post["data"] for post in endpoint.posts}) == 1 and len(endpoint.posts) == 4
    assert AndroidPushStore(home).registrations()[0]["failure_count"] == 0


def test_backoff_jitters_by_twenty_percent():
    deliverer = android_push.Deliverer(jitter=lambda: 0.0, threaded=False)
    assert deliverer._backoff(1) == pytest.approx(1.6)
    deliverer.jitter = lambda: 1.0
    assert deliverer._backoff(5) == pytest.approx(38.4)


def test_five_attempts_then_a_counted_failure(home, clock):
    _register(home, clock)
    endpoint = Endpoint(*[500] * 10)
    _deliverer(clock, endpoint).dispatch(_event(), home)
    assert len(endpoint.posts) == 5 and clock.slept == [2.0, 4.0, 8.0, 16.0]
    assert AndroidPushStore(home).registrations()[0]["failure_count"] == 1


def test_retries_never_outlive_the_ttl(home, clock):
    _register(home, clock)
    endpoint = Endpoint((429, {"retry-after": "100"}))
    locate = events.push_event("location.requested", identifier="locate:l_0123456789abcdef", profile="coder",
                               locate={"id": "l_0123456789abcdef"})
    _deliverer(clock, endpoint).dispatch(locate, home)
    assert len(endpoint.posts) == 1 and clock.slept == []


def test_429_waits_as_told_capped_at_ten_minutes(home, clock):
    _register(home, clock)
    endpoint = Endpoint((429, {"retry-after": "7"}), (429, {"retry-after": "99999"}), 201)
    _deliverer(clock, endpoint).dispatch(_event(), home)
    assert clock.slept == [7.0, 600.0]
    assert android_push.retry_after("Wed, 21 Oct 2015 07:28:10 GMT", 1445412480.0) == 10.0


def test_413_sends_one_wake_up_instead(home, clock):
    _register(home, clock)
    endpoint = Endpoint(413, 413)
    _deliverer(clock, endpoint).dispatch(_event(), home)
    assert len(endpoint.posts) == 2
    assert json.loads(endpoint.posts[1]["data"]) == {"conduit": {"type": "wake", "reason": "too_large"}}


def test_other_client_errors_dont_retry(home, clock):
    _register(home, clock)
    endpoint = Endpoint(400, 201)
    _deliverer(clock, endpoint).dispatch(_event(), home)
    assert len(endpoint.posts) == 1
    assert AndroidPushStore(home).registrations()[0]["failure_count"] == 1


def test_a_week_of_failures_drops_the_registration(home, clock):
    _register(home, clock)
    store = AndroidPushStore(home, clock=clock)
    for _ in range(19):
        assert store.record_failure(INSTALLATION, KID) is False
    clock.now += 7 * 24 * 3600 - 1
    assert store.record_failure(INSTALLATION, KID) is False  # 20 failures, not yet a week
    clock.now += 1
    assert store.record_failure(INSTALLATION, KID) is True
    assert store.registrations() == []


def test_a_success_resets_the_failures(home, clock):
    _register(home, clock)
    store = AndroidPushStore(home, clock=clock)
    store.record_failure(INSTALLATION, KID)
    store.record_success(INSTALLATION, KID)
    [record] = store.registrations()
    assert record["failure_count"] == 0 and record["first_failure_at"] is None and record["last_success_at"] == int(NOW)


def test_a_real_post_carries_the_headers_and_follows_no_redirect(home, clock):
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers["Content-Length"])
            seen.append((self.path, self.headers, self.rfile.read(length)))
            if self.path.startswith("/moved"):
                self.send_response(302)
                self.send_header("Location", "/elsewhere")
            else:
                self.send_response(201)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        AndroidPushStore(home, clock=clock).register(_body(endpoint=f"{base}/up?up=1"), relay_gateway_id=GATEWAY,
                                                    allow_http=True)
        deliverer = android_push.Deliverer(sleep=clock.sleep, clock=clock, threaded=False)
        deliverer.dispatch(_event("approval.needed"), home)
        path, headers, data = seen[0]
        assert path == "/up?up=1" and headers["TTL"] == "86400" and headers["Urgency"] == "high"
        assert headers["Content-Type"] == "application/json" and headers.get("Cookie") is None
        assert headers.get("Authorization") is None
        assert _open(data)[0]["title"] == "Hermes"
        assert AndroidPushStore(home).registrations()[0]["last_success_at"] == int(NOW)

        AndroidPushStore(home, clock=clock).register(_body(endpoint=f"{base}/moved"), relay_gateway_id=GATEWAY,
                                                    allow_http=True)
        deliverer.dispatch(_event(event_id="response:moved"), home)
        assert [entry[0] for entry in seen] == ["/up?up=1", "/moved"]  # never /elsewhere
    finally:
        server.shutdown()


# --- The plugin's send paths -------------------------------------------------


@pytest.fixture()
def profile(tmp_path, monkeypatch, clock):
    monkeypatch.setattr(client, "get_hermes_home", lambda: tmp_path)
    endpoint = Endpoint()
    monkeypatch.setattr(android_push, "deliverer", _deliverer(clock, endpoint))
    relay = []

    def fake_relay(url, *, method, payload=None, credential="", timeout=15.0):
        relay.append(payload)
        return {"accepted": True, "delivered": True}

    monkeypatch.setattr(client, "request_json", fake_relay)
    return types.SimpleNamespace(home=tmp_path, endpoint=endpoint, relay=relay)


def _pair(home, **extra):
    (home / "conduit-push.json").write_text(json.dumps({"credential": "c", "relay_url": "https://relay.example",
                                                        "installation_id": "ios", "gateway_id": "gw", **extra}))


def test_an_android_only_profile_never_talks_to_the_relay(profile, clock):
    _register(profile.home, clock)
    answer = client.send_now(_event())
    assert answer == {"accepted": True, "queued": True, "android": 1}
    assert profile.relay == [] and len(profile.endpoint.posts) == 1
    assert client.enqueue(_event("turn.failed")) is True
    assert profile.relay == [] and len(profile.endpoint.posts) == 2
    assert client.reachable() is True


def test_an_unregistered_unpaired_profile_still_refuses(profile):
    with pytest.raises(RuntimeError):
        client.send_now(_event())
    assert client.enqueue(_event()) is False and client.reachable() is False


def test_what_the_relay_gets_is_unchanged_by_android(profile, clock):
    _pair(profile.home)
    event = _event()
    client.send_now(event)
    without = json.dumps(profile.relay[-1], sort_keys=True)
    _register(profile.home, clock, gateway="gw")
    answer = client.send_now({**event})
    assert json.dumps(profile.relay[-1], sort_keys=True) == without
    assert answer["android"] == 1 and len(profile.endpoint.posts) == 1
    # The Android key never reaches the relay, nor the endpoint the relay.
    assert "ntfy" not in json.dumps(profile.relay)


def test_a_relay_failure_still_says_android_got_it(profile, clock, monkeypatch):
    _pair(profile.home)
    _register(profile.home, clock, gateway="gw")

    def down(*args, **kwargs):
        raise client.RelayRejected(502, "apns_unreachable")

    monkeypatch.setattr(client, "request_json", down)
    with pytest.raises(client.RelayRejected) as raised:
        client.send_now(_event())
    assert raised.value.android == 1


def test_redaction_applies_to_android_too(profile, clock):
    _pair(profile.home, redact_content=True, redact_key="k" * 64)
    _register(profile.home, clock, gateway="gw")
    client.send_now(_event())
    content, _ = _open(profile.endpoint.posts[0]["data"], {"gateway_id": "gw"})
    assert "Deploy to prod?" not in json.dumps(content)


def test_a_call_that_rang_on_android_sends_its_fallback_to_the_relay_only(monkeypatch):
    class Fake:
        def __init__(self):
            self.enqueued = []

        def send_now(self, event):
            error = RuntimeError("not paired")
            error.android = 1
            raise error

        def enqueue(self, event, android=True):
            self.enqueued.append((event["type"], android))
            return False

    fake = Fake()
    monkeypatch.setattr(calls, "client", fake)
    calls.deliver({"type": "call.requested", "event_id": "call:x"}, {"type": "response.ready", "event_id": "r:1"})
    assert fake.enqueued == [("response.ready", False)]


def test_queued_message_is_cancelled_after_unregister_or_key_rotation(home, clock):
    _register(home, clock)
    endpoint = Endpoint()
    delivery = _deliverer(clock, endpoint)
    record = AndroidPushStore(home).registrations()[0]
    job = delivery._job(_event(), record, str(home))
    AndroidPushStore(home).unregister({"installation_id": INSTALLATION})
    assert delivery.deliver(job) == "cancelled"
    _register(home, clock, _body(kid="1" * 32))
    assert delivery.deliver(job) == "cancelled"
    assert endpoint.posts == []


def test_queued_message_is_cancelled_when_notifications_are_disabled(home, clock):
    _register(home, clock)
    endpoint = Endpoint()
    delivery = _deliverer(clock, endpoint)
    job = delivery._job(_event(), AndroidPushStore(home).registrations()[0], str(home))
    _register(home, clock, _body(preferences={"enabled": False}))
    assert delivery.deliver(job) == "cancelled"
    assert endpoint.posts == []


def test_stop_cancels_old_jobs_even_after_start(home, clock):
    _register(home, clock)
    endpoint = Endpoint()
    delivery = _deliverer(clock, endpoint)
    job = delivery._job(_event(), AndroidPushStore(home).registrations()[0], str(home))
    generation = delivery._stop
    delivery.stop()
    assert delivery.dispatch(_event(), home) == 0
    delivery.start()
    assert delivery.deliver(job, stopped=generation) == "cancelled"
    assert delivery.dispatch(_event(), home) == 1
    assert len(endpoint.posts) == 1


def test_android_delivery_never_parks_a_relay_clarify_without_a_relay(profile, clock):
    _register(profile.home, clock)
    event = _event("input.needed", decision={"kind": "clarify", "request_id": "req-1", "question": "Choose?"})
    assert client.enqueue(event) is False
    assert len(profile.endpoint.posts) == 1
    assert profile.relay == []


def test_pulse_level_and_room_invite_routes_are_inside_the_sealed_content(home, clock):
    _register(home, clock)
    endpoint = Endpoint()
    delivery = _deliverer(clock, endpoint)
    assert delivery.dispatch(_event(level="passive"), home) == 1
    content, _ = _open(endpoint.posts[-1]["data"])
    assert content["interruption_level"] == "passive"
    room = {"link": "person-a", "id": "study-room", "name": "Study", "from": "Friend"}
    assert delivery.dispatch(_event("room.invite", room=room), home) == 1
    content, _ = _open(endpoint.posts[-1]["data"])
    assert content["room"] == room
    assert "study-room" not in endpoint.posts[-1]["data"].decode()


def test_old_endpoint_refusal_cannot_remove_its_replacement(home, clock):
    _register(home, clock)
    newer = 'https://ntfy.example/replaced-endpoint?up=1'
    def moved(*args):
        _register(home, clock, _body(endpoint=newer))
        return 410, {}
    delivery = _deliverer(clock, moved)
    assert delivery.dispatch(_event(), home) == 1
    assert AndroidPushStore(home).registrations()[0]['endpoint'] == newer


def test_room_invite_preference_is_independent_of_input_questions(home, clock):
    _register(home, clock, _body(preferences={'input_needed': False, 'room_invite': True}))
    endpoint = Endpoint()
    delivery = _deliverer(clock, endpoint)
    assert delivery.dispatch(_event('input.needed'), home) == 0
    assert delivery.dispatch(_event('room.invite', room={'link':'friend','id':'room'}), home) == 1
    _register(home, clock, _body(preferences={'input_needed': True, 'room_invite': False}))
    assert delivery.dispatch(_event('room.invite', room={'link':'friend','id':'room'}), home) == 0
    assert delivery.dispatch(_event('input.needed'), home) == 1
