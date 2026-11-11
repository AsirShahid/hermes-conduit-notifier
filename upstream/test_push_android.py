"""Android registration through real Hermes authentication and profile routing.

The endpoint is deliberately unreachable and these tests send no push event.
They exercise the installed plugin, not a monkeypatched API router.

The file name matters. This test uses the session-wide `dashboard`, which holds
Hermes' machine host lease until the session ends. test_bot_work.py,
test_compaction_skip.py and test_cron_bot_chat.py start their own backends and
must run before that lease is taken, as they do upstream (the first `dashboard`
user there is test_gateway_listener.py). Named test_android_push.py, this ran
first and every one of their cases failed with "Refusing to start: this host is
already served".
"""
import base64
import json
import secrets
import uuid


def test_android_registration_uses_real_auth_and_isolates_profiles(dashboard):
    status, capabilities = dashboard.get("capabilities")
    assert status == 200
    assert {"android-push", "chat-on-screen-android", "turn-surface-android"}.issubset(capabilities["capabilities"])
    installation = str(uuid.uuid4())
    body = {
        "installation_id": installation,
        "platform": "android-unifiedpush",
        "endpoint": "https://push.invalid/contract/" + installation,
        "e2e": {"v": 1, "kid": secrets.token_hex(16),
                "secret": base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()},
        "preferences": {"enabled": False},
    }
    unregister = {"installation_id": installation, "kid": body["e2e"]["kid"]}
    try:
        status, _ = dashboard.request("POST", "push/android/register", auth=False, body=body)
        assert status in (401, 403)
        for profile in ("default", "second"):
            status, answer = dashboard.request("POST", "push/android/register", body=body, profile=profile)
            assert status == 200
            assert answer["ok"] is True and answer.get("gateway_id")
        status, _ = dashboard.request("POST", "push/android/register", body=body, profile="disabled")
        assert status in (400, 403, 404)
        status, _ = dashboard.request("POST", "push/android/register", body=body, profile="missing")
        assert status in (400, 404)  # Hermes may reject it before the plugin's 400 mapping.
        for profile in ("default", "second"):
            status, safe = dashboard.get("e2e", profile=profile)
            assert status == 200
            rendered = json.dumps(safe)
            assert body["endpoint"] not in rendered and body["e2e"]["secret"] not in rendered
        status, _ = dashboard.request("POST", "push/android/unregister", body=unregister, profile="default")
        assert status == 200
        # Removing default must leave the secondary profile's registration intact.
        kept = json.loads((dashboard.home / "profiles/second/conduit-android-push.json").read_text())
        assert installation in json.dumps(kept)
    finally:
        for profile in ("default", "second"):
            dashboard.request("POST", "push/android/unregister", body=unregister, profile=profile)
