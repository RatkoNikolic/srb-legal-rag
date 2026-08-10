"""Serbian Gajica (Latin) → Ćirilica transliteration, query-side.

The corpus is Cyrillic (convention: never transliterate the *data*), but
users type Latin-script Serbian. The vector leg survives that (multilingual
embeddings map the scripts together); the BM25/FTS leg does not — so FTS
queries get transliterated before search.

Strict standard mapping only: lj→љ, nj→њ, dž→џ + single letters. The
informal "dj"→ђ is deliberately NOT mapped (odjava → *ођава would corrupt
more queries than it fixes). Known ambiguity: compounds like „nadživeti"
(над+живети) transliterate wrongly via the dž digraph — rare in queries,
accepted for v0.
"""

from __future__ import annotations

_DIGRAPHS = {
    "lj": "љ", "Lj": "Љ", "LJ": "Љ",
    "nj": "њ", "Nj": "Њ", "NJ": "Њ",
    "dž": "џ", "Dž": "Џ", "DŽ": "Џ",
}

_SINGLES = {
    "a": "а", "b": "б", "v": "в", "g": "г", "d": "д", "đ": "ђ", "e": "е",
    "ž": "ж", "z": "з", "i": "и", "j": "ј", "k": "к", "l": "л", "m": "м",
    "n": "н", "o": "о", "p": "п", "r": "р", "s": "с", "t": "т", "ć": "ћ",
    "u": "у", "f": "ф", "h": "х", "c": "ц", "č": "ч", "š": "ш",
}
_SINGLES.update({k.upper(): v.upper() for k, v in list(_SINGLES.items())})


def to_cyrillic(text: str) -> str:
    """Transliterate Latin-script Serbian to Cyrillic; Cyrillic and
    non-Serbian characters (digits, punctuation, q/w/x/y) pass through."""
    out: list[str] = []
    i = 0
    while i < len(text):
        pair = text[i : i + 2]
        if pair in _DIGRAPHS:
            out.append(_DIGRAPHS[pair])
            i += 2
            continue
        out.append(_SINGLES.get(text[i], text[i]))
        i += 1
    return "".join(out)
