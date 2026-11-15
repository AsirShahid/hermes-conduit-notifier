"""Device reports kept on this host for automatic triage (dashboard/api/device_reports.py)."""

import copy
import importlib.util
import json
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
BASE = "/api/plugins/conduit_push"


def _load_plugin_api():
    spec = importlib.util.spec_from_file_location("conduit_plugin_api_device_reports", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load_plugin_api()
dr = api.device_reports


def report(build="59", frames=None, kind="hang", exception=None, reason="foreground_stall"):
    frames = frames if frames is not None else [
        "java.lang.String.equals:1678",
        "app.hermesconduit.android.model.ToolCall.equals:39",
        "app.hermesconduit.android.model.ChatMessage.equals:57",
        "androidx.compose.runtime.ComposerImpl.changed:1978",
        "app.hermesconduit.android.ui.ChatScreenKt.Transcript:441",
    ]
    meta = {"appVersion": f"0.1.0-preview.{build}", "appBuildVersion": build, "terminationReason": reason}
    if exception:
        meta["exceptionReason"] = {"className": exception}
    diagnostic = {"version": "Android JVM 1", "diagnosticMetaData": meta, "callStackTree": {"callStackPerThread": False, "callStacks": [
        {"threadAttributed": True, "callStackRootFrames": [{"binaryName": f"JVM {f}", "sampleCount": 1} for f in frames]}]}}
    return {"app_version": f"0.1.0-preview.{build}", "build": build, "os": "Android 17 API 37", "device": "Pixel 11 Pro Fold",
            "kind": kind, "diagnostic": {"timeStampBegin": "2026-11-15T20:41:50.418Z", "timeStampEnd": "2026-11-15T20:41:52.519Z",
                                         "crashDiagnostics": [diagnostic] if kind == "crash" else [],
                                         "hangDiagnostics": [diagnostic] if kind == "hang" else []}}


@pytest.fixture
def root(tmp_path, monkeypatch):
    folder = tmp_path / "conduit-device-reports"
    monkeypatch.setattr(dr, "reports_root", lambda: folder)
    monkeypatch.setattr(dr, "_limiter", api.core._MintLimiter(dr.REPORT_LIMIT, dr.REPORT_WINDOW_S))
    return folder


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    return TestClient(app)


def index(root):
    return json.loads((root / "index.json").read_text())["problems"]


def test_a_report_is_kept_with_a_receipt_conduit_accepts(client, root):
    response = client.post(f"{BASE}/device-reports", json=report())
    assert response.status_code == 200, response.text
    body = response.json()
    # The receipt shape Conduit's transport checks (RelayDeviceReportTransport): 13 digits, a dash, 12 hex.
    assert re.fullmatch(r"[0-9]{13}-[0-9a-f]{12}", body["id"])
    assert body["new"] is True
    kept = json.loads((root / "reports" / f"{body['id']}.json").read_text())
    assert kept["report"]["device"] == "Pixel 11 Pro Fold"
    problem = index(root)[body["signature"]]
    assert problem["status"] == "new" and problem["count"] == 1
    assert problem["title"] == "Freeze in ToolCall.equals"
    assert problem["frames"] == ["app.hermesconduit.android.model.ToolCall.equals",
                                 "app.hermesconduit.android.model.ChatMessage.equals",
                                 "app.hermesconduit.android.ui.ChatScreenKt.Transcript"]


def test_the_same_problem_in_moved_code_is_one_problem(client, root):
    first = client.post(f"{BASE}/device-reports", json=report()).json()
    moved = report(frames=["app.hermesconduit.android.model.ToolCall.equals:52", "app.hermesconduit.android.model.ChatMessage.equals:70",
                           "app.hermesconduit.android.ui.ChatScreenKt.Transcript:460"])
    second = client.post(f"{BASE}/device-reports", json=moved).json()
    assert second["signature"] == first["signature"]
    assert second["new"] is False
    assert index(root)[first["signature"]]["count"] == 2


def test_a_crash_is_titled_by_its_exception(client, root):
    body = client.post(f"{BASE}/device-reports", json=report(kind="crash", reason="java_crash", exception="java.lang.IllegalStateException",
                                                            frames=["app.hermesconduit.android.ui.AppViewModel.resume:1320"])).json()
    assert index(root)[body["signature"]]["title"] == "Crash: IllegalStateException in AppViewModel.resume"


def test_a_fixed_problem_reopens_when_a_newer_build_has_it(root):
    first = dr.record(report(build="59"), root)
    with dr._Locked(root) as idx:
        idx["problems"][first["signature"]].update(status="queued", fixed_after_build=59, note="b02f2aa3")
    again = dr.record(report(build="59"), root)
    assert again["new"] is False and again["status"] == "queued"  # the old build still has it: expected
    back = dr.record(report(build="60"), root)
    assert back["new"] is True
    problem = index(root)[first["signature"]]
    assert problem["status"] == "new" and problem["generation"] == 2
    assert "Came back in build 60" in problem["note"]


def test_an_ignored_problem_stays_ignored(root):
    first = dr.record(report(), root)
    with dr._Locked(root) as idx:
        idx["problems"][first["signature"]]["status"] = "ignored"
    assert dr.record(report(build="61"), root)["new"] is False
    assert index(root)[first["signature"]]["status"] == "ignored"


def test_old_report_files_are_pruned_but_problems_stay(root, monkeypatch):
    monkeypatch.setattr(dr, "KEEP_REPORT_FILES", 3)
    for n in range(6):
        dr.record(report(), root, now=1_763_000_000 + n)
    assert len(list((root / "reports").glob("*.json"))) == 3
    (problem,) = index(root).values()
    assert problem["count"] == 6 and len(problem["reports"]) == 5


@pytest.mark.parametrize("bad", [[], {"kind": "oops"}, {**report(), "diagnostic": {}}, {**report(), "device": None}])
def test_malformed_reports_are_refused(client, root, bad):
    assert client.post(f"{BASE}/device-reports", json=bad).status_code == 400
    assert not (root / "index.json").exists()


def test_an_oversized_report_is_refused(client, root):
    big = copy.deepcopy(report())
    big["os"] = "x" * 30_000
    assert client.post(f"{BASE}/device-reports", json=big).status_code == 413
