"""Corpus registry integrity (the L0–L3 law list, ingestion_pipeline.md §3)."""

import pytest

from lexsr import registry
from lexsr.registry import Role, VersionPolicy


def test_slugs_unique_and_pinned():
    slugs = registry.all_slugs()
    assert len(slugs) == len(set(slugs))  # no duplicate slugs
    for spec in registry.REGISTRY:
        # every law is reachable: a law_guid pin or an ELI path (repealed)
        assert spec.law_guid or spec.eli_path, spec.slug
        # non-repealed laws carry a current regactid pin (pre-flight resolved)
        if not spec.eli_path:
            assert spec.regactid, spec.slug


def test_ten_clusters_plus_backbone():
    clusters = {s.cluster for s in registry.REGISTRY}
    assert clusters == {"backbone", *(str(i) for i in range(1, 11))}


def test_backbone_split_hubs_vs_foundational():
    hubs = registry.hub_slugs()
    assert hubs == {"ustav-rs", "zup", "zoo", "prekrsaji"}  # D-7 down-weighted
    foundational = {s.slug for s in registry.backbone() if s.role is Role.FOUNDATIONAL}
    assert foundational == {"zospo", "javno-beleznistvo"}  # D-9 edges kept live
    for s in registry.REGISTRY:
        assert s.down_weight_in_graph == (s.role is Role.HUB)


def test_radu_is_full_history_showcase():
    assert registry.by_slug("zakon-o-radu").version_policy is VersionPolicy.FULL_HISTORY


def test_repealed_predecessor_via_eli():
    zzpl08 = registry.by_slug("zzpl-2008")
    assert zzpl08.eli_path and zzpl08.version_policy is VersionPolicy.CURRENT_ONLY


def test_resolve_specs_selectors():
    assert {s.slug for s in registry.resolve_specs("1")} == {
        "zakon-o-radu", "bzr", "doprinosi", "pio"
    }
    assert len(registry.resolve_specs("all")) == len(registry.REGISTRY)
    assert [s.slug for s in registry.resolve_specs("zoo,pio")] == ["zoo", "pio"]
    with pytest.raises(KeyError):
        registry.resolve_specs("no-such-law")


def test_ustav_vrsta():
    assert registry.by_slug("ustav-rs").vrsta == "ustav"
