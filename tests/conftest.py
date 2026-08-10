"""Shared fixtures. `make_kg` builds a small on-disk KG using the *production*
SQLite schema from `lexsr.kg` (STRICT + FKs enforced), so arm tests run against
a real store instead of a protocol fake — a fake of an earlier iteration
protocol drifted from the live queries once; a real store can't."""

import sqlite3

import pytest

from lexsr.kg import _SCHEMA, KGStore

_AKT_DEFAULTS = {
    "slug": "", "title": "", "vrsta": "zakon", "role": "seed", "cluster": "",
    "materialized": 1, "in_corpus_depth": 0, "eli": "", "valid_from": "",
    "valid_to": "", "applies_from": "", "repealed_by": "", "down_weight": 0,
}
_CLAN_DEFAULTS = {
    "law_slug": "", "number": "", "fragment": "", "text": "", "chapter": "",
    "structural_path": "", "article_heading": "", "status": "", "valid_from": "",
    "valid_to": "", "applies_from": "", "published": "", "repealed": 0,
    "is_definitions": 0, "is_penalty": 0, "version": "", "is_version_slice": 0,
    "cc_outcome": "", "delegates_to_bylaw": 0,
}


@pytest.fixture
def make_kg(tmp_path):
    """Builder: make_kg(akti=[...], clanovi=[...], upucuje=[(src, dst, mention)],
    defined_in=[(src, dst)], pravni_osnov_za=[(src, dst)],
    ocena_ustavnosti=[(src, dst)], povezan_sa=[(src, dst, down_weight)]) → KGStore.
    `clanovi` entries need chunk_id + source_id; `seq` defaults to list order;
    a `sadrzi` edge is added per clan automatically."""

    def _make(*, akti=(), clanovi=(), upucuje=(), defined_in=(),
              pravni_osnov_za=(), ocena_ustavnosti=(), povezan_sa=()):
        path = tmp_path / "kg.sqlite"
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        for a in akti:
            row = {**_AKT_DEFAULTS, **a}
            conn.execute(
                "INSERT INTO akt VALUES (:source_id,:slug,:title,:vrsta,:role,:cluster,"
                ":materialized,:in_corpus_depth,:eli,:valid_from,:valid_to,:applies_from,"
                ":repealed_by,:down_weight)", row)
        for seq, c in enumerate(clanovi):
            row = {**_CLAN_DEFAULTS, "seq": seq, **c}
            conn.execute(
                "INSERT INTO clan VALUES (:chunk_id,:seq,:source_id,:law_slug,:number,"
                ":fragment,:text,:chapter,:structural_path,:article_heading,:status,"
                ":valid_from,:valid_to,:applies_from,:published,:repealed,:is_definitions,"
                ":is_penalty,:version,:is_version_slice,:cc_outcome,:delegates_to_bylaw)", row)
            conn.execute("INSERT INTO sadrzi VALUES (?,?)", (row["source_id"], row["chunk_id"]))
        conn.executemany("INSERT INTO upucuje_na VALUES (?,?,?)", upucuje)
        conn.executemany("INSERT INTO defined_in VALUES (?,?)", defined_in)
        conn.executemany("INSERT INTO pravni_osnov_za VALUES (?,?)", pravni_osnov_za)
        conn.executemany("INSERT INTO ocena_ustavnosti VALUES (?,?,'')", ocena_ustavnosti)
        conn.executemany("INSERT INTO povezan_sa VALUES (?,?,?)", povezan_sa)
        conn.commit()
        conn.close()
        return KGStore(path)

    return _make
