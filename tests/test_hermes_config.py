"""Conduit's Hermes page routes: built-in memory entries, OpenViking, Hermes files (dashboard/api/hermes_config.py)."""

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"
DELIMITER = "\n§\n"


def _load_plugin_api():
    spec = importlib.util.spec_from_file_location("conduit_plugin_api_hermes_config", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load_plugin_api()


class FakeStore:
    """Hermes' MemoryStore as the routes use it, with its pinned-entry rule (``matched_entry``: only that exact entry,
    else stale) and its budget."""

    def __init__(self, memory=(), user=(), memory_limit=200, user_limit=100, user_enabled=True):
        self.memory_entries = list(memory)
        self.user_entries = list(user)
        self.memory_char_limit = memory_limit
        self.user_char_limit = user_limit
        self.user_profile_enabled = user_enabled

    def target_enabled(self, target):
        return self.user_profile_enabled if target == "user" else True

    def _entries(self, target):
        return self.user_entries if target == "user" else self.memory_entries

    def _limit(self, target):
        return self.user_char_limit if target == "user" else self.memory_char_limit

    def add(self, target, content):
        entries = self._entries(target)
        if len(DELIMITER.join(entries + [content.strip()])) > self._limit(target):
            return {"success": False, "error": "Memory at 190/200 chars. Adding this entry would exceed the limit."}
        entries.append(content.strip())
        return {"success": True, "message": "Entry added."}

    def _pinned(self, target, matched_entry):
        entries = self._entries(target)
        return entries.index(matched_entry) if matched_entry in entries else None

    def replace(self, target, old_text, new_content, matched_entry=None):
        idx = self._pinned(target, matched_entry)
        if idx is None:
            return {"success": False, "error": "The entry changed since it was read; nothing was changed."}
        self._entries(target)[idx] = new_content.strip()
        return {"success": True, "message": "Entry replaced."}

    def remove(self, target, old_text, matched_entry=None):
        idx = self._pinned(target, matched_entry)
        if idx is None:
            return {"success": False, "error": "The entry changed since it was read; nothing was changed."}
        del self._entries(target)[idx]
        return {"success": True, "message": "Entry removed."}


class FakeViking:
    name = "openviking"

    def __init__(self):
        self.calls = []
        self.answers = {}

    def is_available(self):
        return True

    def initialize(self, session_id, **kwargs):
        pass

    def shutdown(self):
        pass

    def handle_tool_call(self, tool, args, **kwargs):
        self.calls.append((tool, args))
        return json.dumps(self.answers.get(tool, {}))


@pytest.fixture
def store(monkeypatch):
    store = FakeStore(memory=["Asir is vegan.", "Knicks fan."], user=["Prefers dark mode."])
    monkeypatch.setattr(api.hermes_config, "_load_store", lambda: store)
    return store


@pytest.fixture
def viking(monkeypatch):
    provider = FakeViking()
    state = types.SimpleNamespace(config={"memory": {"provider": "openviking"}})
    agent_pkg = types.ModuleType("agent")
    memory_provider = types.ModuleType("agent.memory_provider")
    memory_provider.is_core_memory_provider = lambda name: str(name or "").strip().lower() in {
        "", "default", "builtin", "built-in", "none"}
    monkeypatch.setitem(sys.modules, "agent", agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.memory_provider", memory_provider)
    monkeypatch.setattr(api.memory, "_hermes_memory_config", lambda: state.config)
    monkeypatch.setattr(api.memory, "_hermes_load_memory_provider", lambda name: provider if name == "openviking" else None)
    monkeypatch.setattr(api.memory, "_memory_provider_init_kwargs", lambda: {})
    monkeypatch.setattr(api.memory, "_memory_providers", api.memory._MemoryProviders())
    provider.state = state
    return provider


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


# --- Built-in memory ---------------------------------------------------------

def test_memory_lists_each_target_with_its_budget(client, store):
    body = client.get(f"{BASE}/hermes-config/memory").json()
    assert body["ok"] is True
    memory = body["targets"]["memory"]
    assert memory["entries"] == ["Asir is vegan.", "Knicks fan."]
    assert memory["used"] == len("Asir is vegan." + DELIMITER + "Knicks fan.")
    assert memory["limit"] == 200
    assert body["targets"]["user"]["entries"] == ["Prefers dark mode."]


def test_an_edit_replaces_exactly_the_entry_it_names(client, store):
    response = client.post(f"{BASE}/hermes-config/memory",
                           json={"target": "memory", "action": "edit", "entry": "Knicks fan.", "content": "Knicks fan (NBA)."})
    assert response.status_code == 200
    assert store.memory_entries == ["Asir is vegan.", "Knicks fan (NBA)."]
    assert response.json()["memory"]["targets"]["memory"]["entries"][1] == "Knicks fan (NBA)."


def test_an_entry_that_changed_since_it_was_read_is_refused_not_guessed(client, store):
    response = client.post(f"{BASE}/hermes-config/memory",
                           json={"target": "memory", "action": "remove", "entry": "Knicks"})
    assert response.status_code == 409
    assert "changed" in response.json()["detail"]
    assert store.memory_entries == ["Asir is vegan.", "Knicks fan."]


def test_an_add_over_the_budget_is_refused_with_hermes_message(client, store):
    response = client.post(f"{BASE}/hermes-config/memory", json={"target": "user", "action": "add", "content": "x" * 120})
    assert response.status_code == 409
    assert "limit" in response.json()["detail"]
    assert store.user_entries == ["Prefers dark mode."]


def test_add_and_remove(client, store):
    assert client.post(f"{BASE}/hermes-config/memory", json={"target": "user", "action": "add", "content": "Vegan."}).status_code == 200
    assert store.user_entries == ["Prefers dark mode.", "Vegan."]
    assert client.post(f"{BASE}/hermes-config/memory",
                       json={"target": "user", "action": "remove", "entry": "Prefers dark mode."}).status_code == 200
    assert store.user_entries == ["Vegan."]


@pytest.mark.parametrize("body", [
    {"target": "soul", "action": "add", "content": "x"},
    {"target": "memory", "action": "patch", "content": "x"},
    {"target": "memory", "action": "add", "content": "  "},
    {"target": "memory", "action": "edit", "content": "x"},
    [],
])
def test_malformed_changes_are_refused(client, store, body):
    assert client.post(f"{BASE}/hermes-config/memory", json=body).status_code == 400
    assert store.memory_entries == ["Asir is vegan.", "Knicks fan."]


def test_a_turned_off_target_takes_no_writes(client, store):
    store.user_profile_enabled = False
    response = client.post(f"{BASE}/hermes-config/memory", json={"target": "user", "action": "add", "content": "Vegan."})
    assert response.status_code == 409
    assert store.user_entries == ["Prefers dark mode."]


# --- OpenViking -----------------------------------------------------------------

def test_viking_lists_a_folder(client, viking):
    viking.answers["viking_browse"] = {"path": "viking://user/", "entries": [{"uri": "viking://user/a.md", "isDir": False}]}
    body = client.get(f"{BASE}/hermes-config/viking", params={"action": "list", "uri": "viking://user/"}).json()
    assert body["available"] is True
    assert body["entries"][0]["uri"] == "viking://user/a.md"
    assert viking.calls == [("viking_browse", {"action": "list", "path": "viking://user/"})]


def test_viking_search_and_read(client, viking):
    viking.answers["viking_search"] = {"results": [{"uri": "viking://user/a.md", "score": 0.9}], "total": 1}
    viking.answers["viking_read"] = {"content": "Knicks fan."}
    found = client.get(f"{BASE}/hermes-config/viking", params={"action": "search", "q": "  knicks   fan "}).json()
    assert found["results"][0]["uri"] == "viking://user/a.md"
    assert viking.calls[0] == ("viking_search", {"query": "knicks fan", "limit": 30})
    read = client.get(f"{BASE}/hermes-config/viking", params={"action": "read", "uri": "viking://user/a.md"}).json()
    assert read["content"] == "Knicks fan."
    assert viking.calls[1] == ("viking_read", {"uri": "viking://user/a.md", "level": "full"})


def test_viking_forget_is_a_post_through_the_provider(client, viking):
    viking.answers["viking_forget"] = {"status": "deleted", "uri": "viking://user/memories/a.md"}
    assert client.get(f"{BASE}/hermes-config/viking", params={"action": "forget", "uri": "viking://user/memories/a.md"}).status_code == 405
    assert viking.calls == []
    body = client.post(f"{BASE}/hermes-config/viking/forget", json={"uri": "viking://user/memories/a.md"}).json()
    assert body["status"] == "deleted"
    assert viking.calls == [("viking_forget", {"uri": "viking://user/memories/a.md"})]


def test_viking_refusals_reach_the_phone(client, viking):
    viking.answers["viking_forget"] = {"error": "Only memory files can be forgotten."}
    response = client.post(f"{BASE}/hermes-config/viking/forget", json={"uri": "viking://resources/doc"})
    assert response.status_code == 409
    assert "memory files" in response.json()["detail"]


@pytest.mark.parametrize("uri", ["https://example.com", "viking://a\nb", "/home/notes.md"])
def test_viking_takes_only_viking_addresses(client, viking, uri):
    assert client.get(f"{BASE}/hermes-config/viking", params={"action": "read", "uri": uri}).status_code == 400
    assert viking.calls == []


def test_viking_is_unavailable_with_another_provider(client, viking):
    viking.state.config = {"memory": {"provider": "honcho"}}
    body = client.get(f"{BASE}/hermes-config/viking", params={"action": "list"}).json()
    assert body == {"ok": True, "available": False}
    assert viking.calls == []


# --- Hermes files ------------------------------------------------------------------

def test_files_name_the_profile_paths(client, monkeypatch, tmp_path):
    (tmp_path / "SOUL.md").write_text("You are Hermes.")
    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: tmp_path
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    body = client.get(f"{BASE}/hermes-config/files").json()
    files = {row["key"]: row for row in body["files"]}
    assert files["soul"]["path"] == str(tmp_path / "SOUL.md")
    assert files["soul"]["exists"] is True and files["soul"]["size"] == len("You are Hermes.")
    assert files["memory"]["path"] == str(tmp_path / "memories" / "MEMORY.md")
    assert files["memory"]["exists"] is False
