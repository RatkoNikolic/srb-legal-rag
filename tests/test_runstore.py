"""Run-telemetry contract: what goes in comes back out, and aggregates add up."""

import pytest

from kl_core.types import Answer, Provenance
from lexsr.arms.base import ArmResult, RetrievedChunk
from lexsr import runstore


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(runstore, "RUNS_DB", tmp_path / "runs.sqlite3")


def _result(arm: str = "naive", abstained: bool = False, cost: float = 0.01) -> ArmResult:
    citation = Provenance(
        source_id="sgrs/skupstina/zakon/2005/24", source_title="ЗАКОН о раду",
        fragment="član 73", url="https://example.org/eli",
    )
    return ArmResult(
        arm=arm,
        question="Тест питање?",
        as_of="2020-01-01",
        answer=Answer(
            text="" if abstained else "Одговор.",
            citations=[] if abstained else [citation],
            abstained=abstained,
            metadata={"abstain_reason": "нема извора" if abstained else None},
        ),
        retrieved=[RetrievedChunk(
            chunk_id="sgrs/skupstina/zakon/2005/24/clan-73", score=0.9, stage="vector",
            source_title="ЗАКОН о раду", fragment="član 73", text="…",
        )],
        timings_ms={"vector_ms": 30.0, "llm_ms": 900.0, "total_ms": 950.0},
        prompt_tokens=1000, output_tokens=100, cost_usd=cost, model="gemini-3.5-flash",
        extras={"note": "test"},
    )


def test_round_trip():
    run_id = runstore.save_run(_result())
    run = runstore.get_run(run_id)
    assert run is not None
    assert run["question"] == "Тест питање?"
    assert run["as_of"] == "2020-01-01"
    assert run["citations"][0]["fragment"] == "član 73"
    assert run["retrieved"][0]["stage"] == "vector"
    assert run["timings"]["total_ms"] == 950.0
    assert run["abstained"] is False
    assert run["extras"]["note"] == "test"


def test_missing_run_is_none():
    assert runstore.get_run("nonexistent") is None


def test_list_and_stats_aggregate():
    runstore.save_run(_result("naive", cost=0.01))
    runstore.save_run(_result("naive", abstained=True, cost=0.02))
    runstore.save_run(_result("graph", cost=0.04))

    listed = runstore.list_runs()
    assert len(listed) == 3
    assert {"id", "ts", "question", "arm", "abstained", "cost_usd", "total_ms",
            "answer_preview", "as_of"} <= set(listed[0])

    stats = runstore.stats()
    assert stats["total_runs"] == 3
    assert stats["per_arm"]["naive"]["runs"] == 2
    assert stats["per_arm"]["naive"]["abstain_rate"] == 0.5
    assert stats["per_arm"]["graph"]["avg_cost_usd"] == pytest.approx(0.04)
    assert stats["total_cost_usd"] == pytest.approx(0.07)


def _graded(arm, item_id, correct):
    r = _result(arm)
    r.extras["eval_item_id"] = item_id
    return r


def test_batch_grouping_and_graded_analytics():
    # two arms over one item, tagged into a batch and graded
    for arm, corr, prec in (("naive", 1.0, 1.0), ("graph", 0.5, 0.5)):
        run_id = runstore.save_run(_graded(arm, "e01", corr), batch_id="b1")
        runstore.set_eval(run_id, {
            "correct": corr >= 1.0, "correctness": corr, "provenance_fidelity": prec,
            "citation_precision": prec, "citation_recall": prec, "category": "easy",
        })
    # an untagged interactive run groups separately
    runstore.save_run(_result("naive"))

    batches = {b["batch_id"]: b for b in runstore.list_batches()}
    assert batches["b1"]["runs"] == 2 and batches["b1"]["scored"] == 2
    assert "interactive" in batches  # untagged run bucketed

    bs = runstore.batch_stats("b1")["per_arm"]
    assert bs["naive"]["accuracy"] == 1.0
    assert bs["graph"]["accuracy"] == 0.0  # correctness 0.5 → not "correct"
    assert bs["naive"]["citation_precision"] == 1.0

    items = runstore.batch_items("b1")
    assert len(items) == 1 and items[0]["item_id"] == "e01"
    assert items[0]["tier"] == "easy"
    assert items[0]["arms"]["naive"]["correctness"] == 1.0
    assert items[0]["arms"]["graph"]["correctness"] == 0.5


def test_list_runs_filter_by_batch():
    runstore.save_run(_graded("naive", "e01", 1.0), batch_id="b1")
    runstore.save_run(_result("naive"))  # interactive
    assert len(runstore.list_runs(batch_id="b1")) == 1
    assert len(runstore.list_runs(batch_id="interactive")) == 1


def test_golden_set_recorded_and_surfaced_in_batches():
    runstore.save_run(_graded("naive", "e01", 1.0), batch_id="b1", golden="smoke")
    runstore.save_run(_graded("graph", "e01", 1.0), batch_id="b1", golden="smoke")
    run_id = runstore.save_run(_result("naive"))  # interactive, no golden
    assert runstore.get_run(run_id)["golden"] is None

    batches = {b["batch_id"]: b for b in runstore.list_batches()}
    assert batches["b1"]["golden"] == "smoke"
    assert batches["b1"]["goldens"] == ["smoke"]
    assert batches["interactive"]["golden"] is None


def test_golden_column_added_to_legacy_db(tmp_path, monkeypatch):
    """A DB created before the `golden` column gets it forward-migrated."""
    import sqlite3

    db = tmp_path / "legacy.sqlite3"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE runs (id TEXT PRIMARY KEY, ts TEXT, question TEXT, arm TEXT, "
        "batch_id TEXT, conversation_id TEXT, eval_json TEXT, abstained INTEGER DEFAULT 0, "
        "cost_usd REAL DEFAULT 0, timings_json TEXT DEFAULT '{}', extras_json TEXT DEFAULT '{}')"
    )
    con.commit()
    con.close()
    monkeypatch.setattr(runstore, "RUNS_DB", db)
    with runstore._connect() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
    assert "golden" in cols


def test_unfetched_lfs_pointer_is_reported_not_opened(tmp_path, monkeypatch):
    """A clone without git-lfs gets a text stand-in where the DB should be.

    `sqlite3.connect` opens it happily, so without this guard the failure
    surfaces much later as a bare "file is not a database" on the results
    pages. The message has to name the fix, because the cause (git-lfs was
    never installed) is invisible from the symptom.
    """
    import pytest

    from lexsr import runstore

    pointer = tmp_path / "runs.sqlite3"
    pointer.write_bytes(
        b"version https://git-lfs.github.com/spec/v1\n"
        b"oid sha256:abc123\nsize 53555200\n"
    )
    monkeypatch.setattr(runstore, "RUNS_DB", pointer)
    with pytest.raises(RuntimeError, match="git lfs"):
        runstore._connect()

    # a genuinely absent file is NOT an error — a fresh working repo creates it
    monkeypatch.setattr(runstore, "RUNS_DB", tmp_path / "new.sqlite3")
    runstore._connect().close()
