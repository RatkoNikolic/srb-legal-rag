"""Client for the Pravno-informacioni sistem Republike Srbije (PIS).

Legal posture of this ingestion (full record: docs/design/data_sources.md §6/§7):
- Statute texts are not copyright works (Zakon o autorskom i srodnim
  pravima, čl. 6: "Ne smatraju se autorskim delom: 1) zakoni, podzakonski
  akti i drugi propisi; ...").
- Free access to the PIS registry + texts of valid regulations is statutory
  (Zakon o objavljivanju zakona i drugih propisa i akata, Sl. glasnik RS
  45/2013, 10/2023), "bez ograničenja i bez podnošenja posebnog zahteva".
- PIS's site Uslovi korišćenja nonetheless forbid automated
  extraction/indexing/storage and republication without written consent;
  the project proceeds under the documented good-faith position
  (uncopyrightable statute text, re-derived reference graph, polite
  identified crawl, non-commercial research) as an accepted residual
  risk — see data_sources.md §6/§7 for the analysis and handling.

We crawl politely: identified user agent, rate-limited, free section
only. The main site is a JS shell; everything below talks to the backing
APIs it calls (discovered by reading the app bundles — endpoint map in
data_sources.md §1).

PIS identity model (three levels of id — do not conflate them):
- **law guid** — stable identity of a law across reconsolidations
  (`uuid` in registry search results, `hm` in act views).
- **version** — one dated consolidated text of that law: a (`uuid`,
  `regactid`) pair fed to the `viewdoc` API. A law gains a new version
  (new regactid!) every time PIS reconsolidates after an amendment, so
  registry ids stored in code are pins to a *version*, not to the law.
- **lawAdActGuid** — the id ELI paths resolve to; needed to reach laws
  that have dropped out of the valid-regulations registry (repealed laws).
"""

from __future__ import annotations

import time
from datetime import date, datetime
from typing import Any

import httpx2
from pydantic import BaseModel

# API hosts behind the JS shell (config: GET /bandleportalconf.txt, base64 JSON).
REG_API = "https://reg.pravno-informacioni-sistem.rs/api/"
DI_API = "https://di.pravno-informacioni-sistem.rs/"
SLGL_API = "https://slgl.pravno-informacioni-sistem.rs/api/"

VIEWDOC_URL = REG_API + "viewdoc"
USER_AGENT = (
    "lexsr-research/0.1 (srb-legal-rag academic benchmark; contact: ratkonikolic92@gmail.com)"
)

# Seconds between requests. PIS is a public service; one law is a handful of
# requests, the whole v1 corpus is ~100 — there is no reason to hurry.
REQUEST_DELAY_S = 3.0

DOC_TYPE_ZAKON = "53"  # from GET reg/api/standardSearch/filter


class LawVersion(BaseModel):
    """One dated consolidated text (redakcijski prečišćen tekst) of a law.

    `uuid` + `regactid` are the viewdoc parameters for THIS version's text;
    `applies_from` is PIS's versionApplicationDate (when this consolidation
    became the applicable text).
    """

    uuid: str
    regactid: str
    applies_from: date | None = None
    title: str | None = None  # registry title carries the gazette chain


class LawView(BaseModel):
    """A law as the registry sees it: stable guid + version history."""

    law_guid: str
    title: str
    current: LawVersion
    previous: list[LawVersion] = []


class RegistryHit(BaseModel):
    """One row of a registry search: a law guid + its current regactid."""

    law_guid: str
    regactid: str
    title: str  # includes the gazette chain, e.g. "Закон о раду: 24/2005-3, …"
    doc_type: str


class RegistryEntry(BaseModel):
    """Pins one law of the corpus to an exact consolidated version.

    `eli` is the law's canonical ELI URI (PIS implements ELI natively), e.g.
    https://pravno-informacioni-sistem.rs/eli/rep/sgrs/skupstina/zakon/2005/24/1/reg
    Resolve pins with `get_law_view(law_guid)` — regactids change on every
    reconsolidation, so a stored pin is a snapshot, not a permanent address.
    """

    slug: str  # our short name, e.g. "zakon-o-radu"
    title: str
    law_guid: str | None = None
    uuid: str  # version uuid (viewdoc parameter)
    regactid: str  # version regactid (viewdoc parameter)
    eli: str | None = None


# The spike trio + the deliberately-repealed predecessor (build plan §3).
# Resolved 2026-06-11 via the registry APIs. zakon-o-radu deliberately stays
# pinned to the 2018-12-08 consolidation — it is the parser-contract fixture;
# a newer version (regactid 439321, applies from 2026-01-01) exists and is
# reachable via get_law_view(law_guid).
SPIKE_REGISTRY: list[RegistryEntry] = [
    RegistryEntry(
        slug="zakon-o-radu",
        title="Zakon o radu",
        law_guid="694cbad4-4727-4f93-b894-0b100b62c808",
        uuid="c87cb147-7a7b-4dd1-afec-df66ae6da1ca",
        regactid="427266",
        eli="https://pravno-informacioni-sistem.rs/eli/rep/sgrs/skupstina/zakon/2005/24/1/reg",
    ),
    RegistryEntry(
        slug="zakon-o-digitalnoj-imovini",
        title="Zakon o digitalnoj imovini",
        law_guid="fd01348a-d096-4a37-a52b-8d89239cb747",
        uuid="a59b8a78-827e-4d57-b586-5f4a24ea6ae9",
        regactid="431325",
        eli="https://pravno-informacioni-sistem.rs/eli/rep/sgrs/skupstina/zakon/2020/153/1/reg",
    ),
    RegistryEntry(
        slug="zzpl-2018",
        title="Zakon o zaštiti podataka o ličnosti (2018)",
        law_guid="611b2ebf-189d-4421-bf11-b8906ed7ff54",
        uuid="9a49bcd4-c1f0-42b7-9831-5699ed8ad302",
        regactid="427034",
        eli="https://pravno-informacioni-sistem.rs/eli/rep/sgrs/skupstina/zakon/2018/87/13/reg",
    ),
    RegistryEntry(
        # Whole-law-repealed predecessor — the abstain-category target.
        # Not in the valid-regulations registry; resolved via its ELI path
        # (resolve_eli), which still reaches the final consolidated text.
        slug="zzpl-2008",
        title="Zakon o zaštiti podataka o ličnosti (2008, prestao da važi)",
        law_guid="8fc1175b-cd9d-405b-a28e-80624e11932e",
        uuid="1c1d4f50-8f47-4bc8-bc93-6c71ac57fe07",
        regactid="427045",
        eli="https://pravno-informacioni-sistem.rs/eli/rep/sgrs/skupstina/zakon/2008/97/1/reg",
    ),
]

_last_request_at: float = 0.0


def _throttle() -> None:
    global _last_request_at
    wait = REQUEST_DELAY_S - (time.monotonic() - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()


def _request(
    method: str, url: str, *, params: dict | None = None, json: dict | None = None,
    timeout_s: float = 60.0,
) -> httpx2.Response:
    _throttle()
    response = httpx2.request(
        method, url, params=params, json=json,
        headers={"User-Agent": USER_AGENT}, timeout=timeout_s, follow_redirects=True,
    )
    response.raise_for_status()
    return response


def _parse_pis_date(value: str | None) -> date | None:
    return datetime.fromisoformat(value).date() if value else None


def search_registry(
    title_query: str, *, doc_type_ids: list[str] | None = None, limit: int = 20
) -> list[RegistryHit]:
    """Full-text title search over the registry of valid regulations.

    Repealed laws are absent by design — reach those via `resolve_eli`.
    """
    payload: dict[str, Any] = {
        "l": [-1, -1, -1, -1, -1],  # area filter levels, -1 = any
        "pau": None,
        "aids": [],
        "dids": doc_type_ids or [],
        "tmo": True,  # title terms must all occur
        "bmo": True,
        "limit": limit,
        "tk": title_query,
    }
    data = _request("POST", DI_API + "REG/advancedSearch", json=payload).json()
    return [
        RegistryHit(
            law_guid=row["uuid"], regactid=row["id"],
            title=row["title"], doc_type=row["docType"],
        )
        for row in data["result"]
        if row.get("uuid")  # ratified-treaty rows can lack a guid
    ]


def _law_view_from_payload(data: dict, law_guid: str, timeline: dict[str, date]) -> LawView:
    return LawView(
        law_guid=law_guid,
        title=data["title"],
        current=LawVersion(
            uuid=data["url"], regactid=str(data["id"]),
            applies_from=timeline.get(str(data["id"])), title=data["title"],
        ),
        previous=[
            LawVersion(
                uuid=v["url"], regactid=str(v["regId"]),
                applies_from=timeline.get(str(v["regId"])), title=v["name"],
            )
            for v in data.get("previousVersions") or []
        ],
    )


def get_timeline(law_guid: str) -> dict[str, date]:
    """regactid -> date the consolidation became the applicable text."""
    data = _request("GET", REG_API + "GetLawActTimeline", params={"guid": law_guid}).json()
    return {
        str(row["id"]): d
        for row in data
        if (d := _parse_pis_date(row.get("versionApplicationDate"))) is not None
    }


def get_law_view(law_guid: str) -> LawView:
    """Current + previous consolidated versions of a law, with dates."""
    data = _request("GET", REG_API + "GetLawActViewByGuid", params={"guid": law_guid}).json()
    return _law_view_from_payload(data, law_guid, get_timeline(law_guid))


def resolve_eli(eli_path: str) -> LawView:
    """Resolve an ELI path (e.g. "/eli/rep/sgrs/skupstina/zakon/2008/97/1/reg")
    to a LawView. Works for repealed laws, which registry search cannot see."""
    eli_path = "/" + eli_path.split("/eli/", 1)[-1].lstrip("/")
    if not eli_path.startswith("/eli"):
        eli_path = "/eli" + eli_path
    prins = _request("GET", SLGL_API + "prins" + eli_path).json()
    data = _request(
        "GET", REG_API + "GetLawActViewByLawAdActGuid",
        params={"lawAdActGuid": prins["lawAdActGuid"], "eliUrl": prins["eliUrl"]},
    ).json()
    law_guid = data["hm"]
    return _law_view_from_payload(data, law_guid, get_timeline(law_guid))


def get_eli_link(law_guid: str) -> str | None:
    """Canonical ELI URI for a law, or None if PIS has not assigned one."""
    data = _request("GET", REG_API + "GetEliInfoByGuid", params={"guid": law_guid}).json()
    if not data.get("hasEli"):
        return None
    return "https://pravno-informacioni-sistem.rs/" + data["eliLink"].lstrip("/")


def fetch_consolidated_html(
    entry: RegistryEntry | LawVersion, *, timeout_s: float = 60.0
) -> str:
    """Fetch one consolidated text (redakcijski prečišćen tekst) as HTML."""
    response = _request(
        "GET", VIEWDOC_URL, timeout_s=timeout_s,
        params={"uuid": entry.uuid, "regactid": entry.regactid, "doctype": "reg"},
    )
    return response.text


# --- L2/L3 typed reference graph (GetGlobalActReferencesByLawActId) -----------
# Probed live 2026-06-20 (docs/engineering/ingestion_pipeline.md §18). The
# endpoint returns {category: {actReferenceTypeName, lawActs:[{id, name,
# inlineUrl, url}]}}. `id` is the regActId; `inlineUrl` carries the host — the
# fetch-routing key: bylaws/related/Ustav on `reg.` (viewDoc), but CC decisions,
# authentic interpretations and gazette amendment acts only on `slgl.` (Prins).
GLOBAL_REFS_URL = REG_API + "GetGlobalActReferencesByLawActId"

# PIS reference category → KG edge type (ingestion_pipeline.md §5.3 / D-8/D-9).
REFERENCE_EDGES: dict[str, str] = {
    "osnovDonosenjaPropisa": "USTAVNI_OSNOV",        # this law → its enactment basis (Ustav)
    "aktKojimaJePropisOsnovDonosenja": "PRAVNI_OSNOV_ZA",  # this law → bylaws under it
    "ustavnoSudskeOdluke": "OCENA_USTAVNOSTI",       # CC decision → this law (validity)
    "povezaniPropis": "POVEZAN_SA",                  # related
    "aktiSaKojimaJePropisPovezan": "POVEZAN_SA",     # connected
    "aktiKojiSuNaPropisUticali": "UTICAO_NA",        # acts that affected this law
    # osnovniTekstIzmeneDopuneIspravke is the amendment/version chain — handled
    # via the version timeline, not as a flat reference edge.
}


class ActReference(BaseModel):
    """One typed reference to another act, from GetGlobalActReferencesByLawActId."""

    category: str  # the PIS category key
    edge: str  # mapped KG edge type (REFERENCE_EDGES)
    ref_id: str  # the referenced act's regActId (its identity)
    name: str
    inline_url: str  # ready fetch address (carries the host)

    @property
    def host(self) -> str:
        return "slgl" if "slgl." in self.inline_url else "reg" if "reg." in self.inline_url else "?"


def get_references(reg_act_id: str) -> dict[str, list[ActReference]]:
    """Typed reference graph for one law version, keyed by KG edge type.

    Categories not in `REFERENCE_EDGES` (the amendment chain) are skipped — those
    are reconstructed from the version timeline. Items without a usable id/url are
    dropped (defensive: ratified-treaty / placeholder rows occasionally appear).
    """
    data = _request("GET", GLOBAL_REFS_URL, params={"lawActId": reg_act_id}).json()
    out: dict[str, list[ActReference]] = {}
    for category, edge in REFERENCE_EDGES.items():
        block = data.get(category)
        acts = (block or {}).get("lawActs") if isinstance(block, dict) else None
        for act in acts or []:
            ref_id = str(act.get("id") or "").strip()
            inline = (act.get("inlineUrl") or "").strip()
            if not ref_id or not inline:
                continue
            out.setdefault(edge, []).append(
                ActReference(
                    category=category, edge=edge, ref_id=ref_id,
                    name=(act.get("name") or "").strip(), inline_url=inline,
                )
            )
    return out


def fetch_bytes(url: str, *, timeout_s: float = 60.0) -> tuple[bytes, str]:
    """Fetch a raw asset (returns (content, content_type)). Used for image
    annexes (`Attachment/slike/…`) that the OCR branch transcribes."""
    r = _request("GET", url, timeout_s=timeout_s)
    return r.content, r.headers.get("content-type", "")


def fetch_doc(url: str, *, timeout_s: float = 60.0) -> str:
    """Fetch a referenced document's HTML by its `inlineUrl`. The URL already
    encodes the correct host (reg `viewDoc` or slgl `Prins`), so we GET it
    directly — the routing the pre-flight showed is mandatory (the `reg` host
    returns a 31-byte stub for `slgl`-hosted acts)."""
    return _request("GET", url, timeout_s=timeout_s).text
