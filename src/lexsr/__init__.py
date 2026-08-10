"""The Serbian-legal domain skin over kl-core.

`lexsr.pis`    — client for the Pravno-informacioni sistem RS (official, free
                 by statute; see the licensing note in `pis.py`).
`lexsr.parser` — consolidated-text HTML → structured Zakon/Član/Stav with
                 gazette amendment chains and provision-level change notes.
"""

from lexsr.parser import ConsolidatedLaw, parse_consolidated_html, to_chunks

__all__ = ["ConsolidatedLaw", "parse_consolidated_html", "to_chunks"]
