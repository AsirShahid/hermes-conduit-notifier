"""Model usage for Conduit's Favorites (dashboard/api/model_usage.py)."""

import importlib.util
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"
DAY = 86400.0
NOW = 1_800_000_000.0


def _load_plugin_api():
    spec = importlib.util.spec_from_file_location("conduit_plugin_api_model_usage", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load_plugin_api()
mu = api.model_usage


def make_db(path: Path, rows):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE session_model_usage (session_id TEXT, model TEXT, billing_provider TEXT, task TEXT DEFAULT '', "
        "api_call_count INTEGER, first_seen REAL, last_seen REAL)")
    for index, row in enumerate(rows):
        seen = NOW - row.get("days", 0) * DAY
        conn.execute("INSERT INTO session_model_usage VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (f"s{index}", row.get("model"), row.get("provider"), row.get("task", ""), row.get("requests", 1), seen - 60, seen))
    conn.commit()
    conn.close()
    return path


def by_key(result):
    return {(r["provider"], r["model"]): r for r in result["rows"]}


def test_requests_count_for_the_provider_that_served_them(tmp_path):
    # Asir, 2026-11-15: "GPT 6 from command code should definitely not be one of my most used." A fallback to
    # CommandCode is its own small row, not his regular-codex chats relabelled.
    db = make_db(tmp_path / "state.db", [
        {"model": "gpt-6-astra", "provider": "custom:regular-codex", "requests": 300, "days": 1},
        {"model": "gpt-6-astra", "provider": "commandcode", "requests": 4, "days": 1},
        {"model": "gpt-6-astra", "provider": "custom:regular-codex", "requests": 100, "days": 1},
    ])
    result = mu.usage(db, NOW)
    rows = by_key(result)
    assert rows[("custom:regular-codex", "gpt-6-astra")]["requests"] == 400
    assert rows[("commandcode", "gpt-6-astra")]["requests"] == 4
    assert [r["provider"] for r in result["rows"]] == ["custom:regular-codex", "commandcode"]


def test_requests_halve_every_seven_days_and_models_unused_for_two_weeks_drop(tmp_path):
    # Asir: GPT-6 Luna, 5.6 Luna and 5.6 Sol "wouldn't be up there. Even though they're 3 of my top 4 in the past 4
    # months ... none have been used at all in the past few weeks".
    db = make_db(tmp_path / "state.db", [
        {"model": "gpt-5.6-sol", "provider": "openai-codex", "requests": 50_000, "days": 40},
        {"model": "gpt-5.6-sol", "provider": "openai-codex", "requests": 2_000, "days": 17},
        {"model": "gpt-6-luna", "provider": "custom:regular-codex", "requests": 3_000, "days": 10},
        {"model": "claude-fable-5-2", "provider": "anthropic", "requests": 600, "days": 0},
        {"model": "claude-opus-5-5", "provider": "anthropic", "requests": 100, "days": 7},
    ])
    result = mu.usage(db, NOW)
    assert (result["half_life_days"], result["recent_days"], result["window_days"]) == (7, 14, 90)
    assert [r["model"] for r in result["rows"]] == ["gpt-6-luna", "claude-fable-5-2", "claude-opus-5-5"]
    rows = by_key(result)
    assert rows[("custom:regular-codex", "gpt-6-luna")]["weight"] == pytest.approx(3_000 * 0.5 ** (10 / 7), abs=0.01)
    assert rows[("anthropic", "claude-opus-5-5")]["weight"] == pytest.approx(50, abs=0.01)
    assert rows[("anthropic", "claude-fable-5-2")]["latest"] == int(NOW * 1000)


def test_auxiliary_work_old_rows_and_blanks_do_not_count(tmp_path):
    db = make_db(tmp_path / "state.db", [
        {"model": "claude-haiku-5-5", "provider": "anthropic", "task": "title_generation", "requests": 900},
        {"model": "gemini-3-flash", "provider": "google", "task": "vision", "requests": 900},
        {"model": "a", "provider": "p", "task": "compression"},
        {"model": "old", "provider": "p", "days": 91},
        {"model": "", "provider": "p"},
        {"model": "none", "provider": "p", "requests": 0},
        {"model": "kept", "provider": None, "requests": 2},
    ])
    assert [(r["provider"], r["model"]) for r in mu.usage(db, NOW)["rows"]] == [(None, "kept")]


def test_no_state_db_or_no_ledger_is_no_usage(tmp_path):
    assert mu.usage(tmp_path / "missing.db", NOW)["rows"] == []
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    assert mu.usage(empty, NOW)["rows"] == []


def test_the_route_answers_for_the_profile(tmp_path, monkeypatch):
    db = make_db(tmp_path / "state.db", [{"model": "claude-opus-5-5", "provider": "anthropic", "requests": 5}])
    monkeypatch.setattr(mu, "state_db", lambda: db)
    monkeypatch.setattr(mu, "_limiter", api.core._MintLimiter(mu.USAGE_LIMIT, mu.USAGE_WINDOW_S))
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    response = TestClient(app).get(f"{BASE}/model-usage")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    row = body["rows"][0]
    assert (row["provider"], row["model"], row["requests"]) == ("anthropic", "claude-opus-5-5", 5)
    assert response.headers["cache-control"] == "no-store"


def test_capability_is_advertised():
    assert "model-usage" in api.capabilities.ROUTE_CAPABILITIES
