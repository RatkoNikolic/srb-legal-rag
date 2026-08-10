"""The srb-legal-rag corpus registry — the single source of truth for *which*
laws the knowledge base is built from, and *how* each is ingested.

Design reference: docs/engineering/ingestion_pipeline.md §3 (law selection) +
§13 (decision log). The corpus is **10 self-contained thematic clusters + a
shared backbone**, depth over breadth (D-4, D-5, D-10).

- **Seeds** are the topical center of a cluster.
- **Backbone** laws are horizontal connective tissue, materialized once and
  reused by every cluster — split into *hubs* (Ustav/ZUP/ZOO/prekršaji:
  high-in-degree, their reference edges are down-weighted in graph traversal —
  D-7) and *foundational* laws (ZOSPO/notary: cross-cutting but their edges are
  informative, so kept live — D-9).

Pins (`law_guid` + current `regactid`) were resolved live against PIS on
2026-06-20 (pre-flight, ingestion_pipeline.md §18 — 38/39 exact matches).
`regactid` is a *version* pin and drifts on reconsolidation (PIS D7); it is a
snapshot, re-resolved at ingest time via `get_law_view(law_guid)`.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class Role(str, Enum):
    SEED = "seed"  # topical center of a cluster
    HUB = "hub"  # backbone hub — reference edges down-weighted in graph traversal
    FOUNDATIONAL = "foundational"  # backbone, cross-cutting, edges kept live


class VersionPolicy(str, Enum):
    CURRENT_ONLY = "current_only"  # one version (current consolidation)
    CHANGED_SLICES = "changed_slices"  # current-full + changed-article slices (Fork B)
    FULL_HISTORY = "full_history"  # every version PIS serves (temporal showcase)


class LawSpec(BaseModel):
    """One law in the corpus and how to ingest it."""

    slug: str
    cluster: str  # cluster id ("1".."10") or "backbone"
    role: Role
    vrsta: str = "zakon"  # Akt discriminator (zakon / ustav / …)
    law_guid: str | None = None
    regactid: str | None = None  # current-version pin (snapshot; re-resolved)
    query: str | None = None  # registry title search fallback (Cyrillic)
    eli_path: str | None = None  # for laws invisible to registry search (repealed)
    version_policy: VersionPolicy = VersionPolicy.CHANGED_SLICES

    @property
    def down_weight_in_graph(self) -> bool:
        """Hub backbone laws are graph stop-words — their reference edges are
        down-weighted / excluded from 2-hop expansion (D-7)."""
        return self.role is Role.HUB


CLUSTER_NAMES: dict[str, str] = {
    "backbone": "Shared backbone",
    "1": "Employment & social contributions",
    "2": "Family & inheritance",
    "3": "Real estate, housing & construction",
    "4": "Consumer & everyday transactions",
    "5": "Traffic & misdemeanors",
    "6": "Personal taxes & freelancing",
    "7": "Personal data & the digital sphere",
    "8": "Business & company life (SME)",
    "9": "Civil disputes, enforcement & legal aid",
    "10": "Health, social welfare & family benefits",
}

_S = Role.SEED
_H = Role.HUB
_F = Role.FOUNDATIONAL
_CHANGED = VersionPolicy.CHANGED_SLICES
_CURRENT = VersionPolicy.CURRENT_ONLY
_FULL = VersionPolicy.FULL_HISTORY


REGISTRY: list[LawSpec] = [
    # ---- shared backbone (materialized once, reused by every cluster) --------
    LawSpec(slug="ustav-rs", cluster="backbone", role=_H, vrsta="ustav",
            law_guid="f0d72ce3-dad2-4395-a2c1-c3d3226f22c2", regactid="433539",
            query="устав републике србије", version_policy=_CURRENT),
    LawSpec(slug="zup", cluster="backbone", role=_H,
            law_guid="3544aef0-85fd-4733-8214-e8bb95edf4af", regactid="434986",
            query="закон о општем управном поступку"),
    LawSpec(slug="zoo", cluster="backbone", role=_H,
            law_guid="70677d4a-d9fa-4954-aa38-a71ae86a3e61", regactid="429975",
            query="закон о облигационим односима", version_policy=_CURRENT),
    LawSpec(slug="prekrsaji", cluster="backbone", role=_H,
            law_guid="1525d7d7-95d3-4a34-9f3d-05cf7ae73450", regactid="434479",
            query="закон о прекршајима"),
    LawSpec(slug="zospo", cluster="backbone", role=_F,
            law_guid="c993c999-d54c-4781-bdcd-46b506c096fe", regactid="421888",
            query="закон о основама својинскоправних односа", version_policy=_CURRENT),
    LawSpec(slug="javno-beleznistvo", cluster="backbone", role=_F,
            law_guid="21edb4de-211f-4d2f-b2c7-e6a8622227e5", regactid="437840",
            query="закон о јавном бележништву"),
    # ---- 1 · Employment & social contributions ------------------------------
    LawSpec(slug="zakon-o-radu", cluster="1", role=_S,
            law_guid="694cbad4-4727-4f93-b894-0b100b62c808", regactid="439321",
            query="закон о раду", version_policy=_FULL),  # temporal showcase
    LawSpec(slug="bzr", cluster="1", role=_S,
            law_guid="997cf175-3953-49e2-8e45-a60126a31b4b", regactid="435409",
            query="закон о безбедности и здрављу на раду"),
    LawSpec(slug="doprinosi", cluster="1", role=_S,
            law_guid="04455352-c6cf-4ce5-9a7a-2d75505518f5", regactid="439654",
            query="закон о доприносима за обавезно социјално осигурање"),
    LawSpec(slug="pio", cluster="1", role=_S,
            law_guid="02269aae-7bf8-433e-9e30-2420414fb9fa", regactid="437816",
            query="закон о пензијском и инвалидском осигурању"),
    # ---- 2 · Family & inheritance -------------------------------------------
    LawSpec(slug="porodicni", cluster="2", role=_S,
            law_guid="78346cae-e351-4fa4-9244-af7d09d39413", regactid="439358",
            query="породични закон"),
    LawSpec(slug="nasledjivanje", cluster="2", role=_S,
            law_guid="a6e688af-af55-40de-9df8-e376bbc53c91", regactid="401956",
            query="закон о наслеђивању"),
    LawSpec(slug="maticne-knjige", cluster="2", role=_S,
            law_guid="a630eb8a-9e22-446e-a9c4-87882746020f", regactid="426159",
            query="закон о матичним књигама"),
    # ---- 3 · Real estate, housing & construction ----------------------------
    LawSpec(slug="promet-nepokretnosti", cluster="3", role=_S,
            law_guid="ba4fe13c-d36b-4ea6-89c2-397a069f04df", regactid="423437",
            query="закон о промету непокретности"),
    LawSpec(slug="planiranje-izgradnja", cluster="3", role=_S,
            law_guid="59adbea5-171d-4714-a17e-0f9ae1b04fa5", regactid="439111",
            query="закон о планирању и изградњи"),
    LawSpec(slug="stanovanje", cluster="3", role=_S,
            law_guid="91835ea1-108a-42e6-aab0-a75501aef129", regactid="429829",
            query="закон о становању и одржавању зграда"),
    LawSpec(slug="katastar", cluster="3", role=_S,
            law_guid="c30f3576-9c8c-4d16-b640-73d5fa9f04a6", regactid="439114",
            query="закон о државном премеру и катастру"),
    # ozakonjenje: unresolved at pre-flight (ingestion_pipeline.md §18) — add
    # once the current title is confirmed.
    # ---- 4 · Consumer & everyday transactions -------------------------------
    LawSpec(slug="zastita-potrosaca", cluster="4", role=_S,
            law_guid="d6b5b3e2-5558-4a50-bf54-ffa0debd6a84", regactid="432708",
            query="закон о заштити потрошача"),
    LawSpec(slug="trgovina", cluster="4", role=_S,
            law_guid="ae9a5bc3-dfd1-428b-9c3b-c9232579399c", regactid="439979",
            query="закон о трговини"),
    LawSpec(slug="finansijske-usluge", cluster="4", role=_S,
            law_guid="d817287f-5a95-47bb-a31f-bc2fd89b784a", regactid="438305",
            query="закон о заштити корисника финансијских услуга"),
    # ---- 5 · Traffic & misdemeanors -----------------------------------------
    LawSpec(slug="bezbednost-saobracaja", cluster="5", role=_S,
            law_guid="8b57d1e9-9b34-4fa0-9693-7ceb6da65486", regactid="438311",
            query="закон о безбедности саобраћаја на путевима"),
    # ---- 6 · Personal taxes & freelancing -----------------------------------
    LawSpec(slug="porez-dohodak", cluster="6", role=_S,
            law_guid="a508349c-94de-4e51-93a6-85c0b155688a", regactid="439651",
            query="закон о порезу на доходак грађана"),
    LawSpec(slug="porez-imovina", cluster="6", role=_S,
            law_guid="4aa3e2f1-47c7-4b20-b83f-c52d5a5c6b11", regactid="437849",
            query="закон о порезима на имовину"),
    LawSpec(slug="zpppa", cluster="6", role=_S,
            law_guid="867665ea-a0a7-46e1-8dcb-cf9ab3465511", regactid="439353",
            query="закон о пореском поступку и пореској администрацији"),
    # ---- 7 · Personal data & the digital sphere -----------------------------
    LawSpec(slug="zzpl-2018", cluster="7", role=_S,
            law_guid="611b2ebf-189d-4421-bf11-b8906ed7ff54", regactid="427034",
            query="закон о заштити података о личности"),
    LawSpec(slug="zzpl-2008", cluster="7", role=_S, version_policy=_CURRENT,
            law_guid="8fc1175b-cd9d-405b-a28e-80624e11932e",
            eli_path="/eli/rep/sgrs/skupstina/zakon/2008/97/1/reg"),  # repealed predecessor
    LawSpec(slug="digitalna-imovina", cluster="7", role=_S,
            law_guid="fd01348a-d096-4a37-a52b-8d89239cb747", regactid="431325",
            query="закон о дигиталној имовини"),
    # ---- 8 · Business & company life (SME) ----------------------------------
    LawSpec(slug="privredna-drustva", cluster="8", role=_S,
            law_guid="4e4707d8-058b-4512-9a53-a93c38c4c33f", regactid="438316",
            query="закон о привредним друштвима"),
    LawSpec(slug="apr-registracija", cluster="8", role=_S,
            law_guid="5bef4f36-864f-45aa-a1e1-561b131cc679", regactid="432979",
            query="закон о поступку регистрације у агенцији за привредне регистре"),
    LawSpec(slug="racunovodstvo", cluster="8", role=_S,
            law_guid="0e6f0c88-c8f5-4a42-ab07-688c37807d18", regactid="432123",
            query="закон о рачуноводству"),
    LawSpec(slug="stecaj", cluster="8", role=_S,
            law_guid="16b0c51c-8235-4870-a5f8-887ba54b006c", regactid="438645",
            query="закон о стечају"),
    # ---- 9 · Civil disputes, enforcement & legal aid ------------------------
    LawSpec(slug="parnicni", cluster="9", role=_S,
            law_guid="ff7a2508-60fe-41f6-a66b-b538c3d17021", regactid="435085",
            query="закон о парничном поступку"),
    LawSpec(slug="izvrsenje", cluster="9", role=_S,
            law_guid="a9b55f5c-d912-4c02-8aaa-5afb937ca1dd", regactid="439115",
            query="закон о извршењу и обезбеђењу"),
    LawSpec(slug="vanparnicni", cluster="9", role=_S,
            law_guid="92a242bd-b800-4a84-8a85-fecab3871261", regactid="433526",
            query="закон о ванпарничном поступку"),
    LawSpec(slug="besplatna-pravna-pomoc", cluster="9", role=_S,
            law_guid="85342e7d-9327-44e2-97b1-247229021688", regactid="427046",
            query="закон о бесплатној правној помоћи"),
    # ---- 10 · Health, social welfare & family benefits ----------------------
    LawSpec(slug="zdravstveno-osiguranje", cluster="10", role=_S,
            law_guid="c48bc4c1-31f2-485a-a483-631bece24d53", regactid="439325",
            query="закон о здравственом осигурању"),
    LawSpec(slug="zdravstvena-zastita", cluster="10", role=_S,
            law_guid="700e4b89-d6d0-40e7-97ad-45994460ef77", regactid="438506",
            query="закон о здравственој заштити"),
    LawSpec(slug="socijalna-zastita", cluster="10", role=_S,
            law_guid="e012df69-7bc7-47ec-9f62-38353c561e46", regactid="434531",
            query="закон о социјалној заштити"),
    LawSpec(slug="podrska-porodici", cluster="10", role=_S,
            law_guid="a5e830e3-3f94-4ea9-a291-d474793c401f", regactid="437632",
            query="закон о финансијској подршци породици са децом"),
]

_BY_SLUG = {spec.slug: spec for spec in REGISTRY}


def by_slug(slug: str) -> LawSpec:
    return _BY_SLUG[slug]


def all_slugs() -> list[str]:
    return [s.slug for s in REGISTRY]


def by_cluster(cluster: str) -> list[LawSpec]:
    return [s for s in REGISTRY if s.cluster == cluster]


def backbone() -> list[LawSpec]:
    return [s for s in REGISTRY if s.role in (Role.HUB, Role.FOUNDATIONAL)]


def seeds() -> list[LawSpec]:
    return [s for s in REGISTRY if s.role is Role.SEED]


def hub_slugs() -> set[str]:
    """Backbone hubs whose reference edges are down-weighted in graph traversal."""
    return {s.slug for s in REGISTRY if s.role is Role.HUB}


def resolve_specs(selector: str) -> list[LawSpec]:
    """Resolve a CLI selector to law specs: 'all', a cluster id ('1'..'10' or
    'backbone'), or a comma-separated slug list."""
    if selector == "all":
        return list(REGISTRY)
    if selector in CLUSTER_NAMES:
        return by_cluster(selector)
    specs = []
    for slug in (s.strip() for s in selector.split(",") if s.strip()):
        if slug not in _BY_SLUG:
            raise KeyError(f"unknown law slug: {slug!r}")
        specs.append(_BY_SLUG[slug])
    return specs
