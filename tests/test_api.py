"""API validation contracts (no model key, no index needed)."""

import pytest
from fastapi.testclient import TestClient

from lexsr import bench, runstore
from lexsr.api import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(runstore, "RUNS_DB", tmp_path / "runs.sqlite3")
    monkeypatch.setattr(bench, "CURRENT_GOLDEN", tmp_path / "golden.jsonl")
    monkeypatch.setattr(bench, "_nonempty_cache", {"src/x/clan-1"})
    # patch BOTH caches — validate_item→_chunk_texts would otherwise load the
    # real 25k-chunk corpus mid-test and overwrite the patched global
    monkeypatch.setattr(bench, "_chunk_text_cache", {"src/x/clan-1": "Одговор."})


def test_arms_listing():
    response = client.get("/api/arms")
    assert response.status_code == 200
    assert response.json() == ["advanced", "agentic", "context-stuffing", "graph", "naive"]


def test_ablations_surface():
    body = client.get("/api/ablations").json()
    keys = {t["key"] for t in body["spec"]}
    assert keys == set(body["defaults"])  # the UI surface matches the config fields
    assert all(body["defaults"].values())  # every default is the full-pipeline setting
    assert {"rerank", "delegation_gate", "temporal_filter"} <= keys


def test_unknown_ablation_toggle_is_422():
    # extra=forbid on AblationConfig → an unknown toggle is a validation error
    response = client.post(
        "/api/ask",
        json={"question": "Питање?", "arm": "naive", "ablation": {"made_up": True}},
    )
    assert response.status_code == 422


def test_empty_question_is_422():
    response = client.post("/api/ask", json={"question": "   ", "arm": "naive"})
    assert response.status_code == 422


def test_unknown_arm_is_404():
    response = client.post("/api/ask", json={"question": "Питање?", "arm": "quantum"})
    assert response.status_code == 404


def test_bad_as_of_is_422():
    response = client.post(
        "/api/ask", json={"question": "Питање?", "arm": "naive", "as_of": "not-a-date"}
    )
    assert response.status_code == 422


def test_unknown_run_is_404():
    assert client.get("/api/runs/zzz").status_code == 404


def test_empty_stores():
    assert client.get("/api/runs").json() == []
    stats = client.get("/api/stats").json()
    assert stats["total_runs"] == 0
    assert client.get("/api/corpus").status_code == 200
    assert client.get("/api/batches").json() == []


def test_golden_crud_roundtrip():
    item = {
        "item_id": "", "category": "easy",
        "query": {"text": "Питање?", "filters": {"law_slug": "x"}},
        "gold_answer": "Одговор.", "gold_citations": ["src/x::član 1"],
    }
    created = client.put("/api/golden", json=item).json()
    assert created["item_id"] == "e01"
    assert len(client.get("/api/golden").json()) == 1
    assert client.delete("/api/golden/e01").status_code == 200
    assert client.delete("/api/golden/e01").status_code == 404


def test_golden_validation_is_422():
    bad = {"item_id": "", "category": "easy",
           "query": {"text": "Питање?"}, "gold_answer": "x",
           "gold_citations": ["src/x::član 999"]}  # ungrounded citation
    assert client.put("/api/golden", json=bad).status_code == 422


def test_benchmark_run_rejects_unknown_arm():
    resp = client.post("/api/benchmark/run", json={"arms": ["quantum"]})
    assert resp.status_code == 422


def test_benchmark_run_rejects_bad_golden_set():
    resp = client.post("/api/benchmark/run", json={"arms": ["naive"], "golden": ".."})
    assert resp.status_code == 422


def test_golden_sets_and_named_set_get(monkeypatch, tmp_path):
    monkeypatch.setattr(bench, "GOLDENS_DIR", tmp_path)
    monkeypatch.setattr(bench, "CURRENT_GOLDEN", tmp_path / "main.jsonl")
    (tmp_path / "main.jsonl").write_text('{"item_id": "e01"}\n', encoding="utf-8")
    (tmp_path / "other.jsonl").write_text('{"item_id": "x01"}\n', encoding="utf-8")

    sets = client.get("/api/golden/sets").json()
    assert {s["name"] for s in sets} == {"main", "other"}
    assert next(s for s in sets if s["name"] == "main")["is_default"]
    # ?golden= scopes the item listing to the named set
    assert client.get("/api/golden?golden=other").json() == [{"item_id": "x01"}]
