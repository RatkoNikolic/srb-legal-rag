"""Diversity + version-slice routing primitives (lexsr.arms.base)."""

from __future__ import annotations

from datetime import date

from lexsr.arms.base import _act_of, cap_per_act, route_slices


def test_act_of_strips_suffixes():
    assert _act_of("sgrs/.../zakon/2005/24/1/clan-179") == "sgrs/.../zakon/2005/24/1"
    assert _act_of("sgrs/.../zakon/2005/24/1/clan-179@427266") == "sgrs/.../zakon/2005/24/1"
    assert _act_of("ref/437010/prilog-ocr-2") == "ref/437010"
    assert _act_of("ref/893049/holding") == "ref/893049"


def test_cap_per_act_limits_per_source():
    rows = [
        {"chunk_id": "law-a/clan-1"},
        {"chunk_id": "law-a/clan-2"},
        {"chunk_id": "law-a/clan-3"},
        {"chunk_id": "law-a/clan-4"},   # 4th of law-a — dropped at cap=3
        {"chunk_id": "law-b/clan-1"},
    ]
    out = cap_per_act(rows, cap=3, id_of=lambda r: r["chunk_id"])
    ids = [r["chunk_id"] for r in out]
    assert ids == ["law-a/clan-1", "law-a/clan-2", "law-a/clan-3", "law-b/clan-1"]


def test_route_slices_drops_slices_without_as_of():
    rows = [
        {"chunk_id": "a/clan-1", "is_version_slice": False},
        {"chunk_id": "a/clan-1@v1", "is_version_slice": True},
    ]
    # no as_of → current text only
    assert [r["chunk_id"] for r in route_slices(rows, None)] == ["a/clan-1"]
    # with as_of → keep both (in_force_on selects the right one downstream)
    assert len(route_slices(rows, date(2019, 1, 1))) == 2
