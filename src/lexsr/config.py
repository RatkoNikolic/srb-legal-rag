"""Runtime configuration: .env-driven, no secrets in code or git.

`GEMINI_API_KEY` lives in the repo-root .env (gitignored). Data artifacts live
under data/ and are **tracked** so a `git pull` resumes work on any machine
(text source-of-record in plain git; the index/KG/telemetry binaries via Git
LFS — see .gitattributes). Only scratch (`data/raw/`, `data/evals/`) is ignored.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"
CORPUS_DIR = DATA_DIR / "corpus"
INDEX_DIR = DATA_DIR / "index"
KG_PATH = DATA_DIR / "kg.sqlite"
RUNS_DB = DATA_DIR / "runs.sqlite3"
EVALS_DIR = DATA_DIR / "evals"
GOLDENS_DIR = REPO_ROOT / "goldens"  # committed: ground truth is source, not artifact
CURRENT_GOLDEN = GOLDENS_DIR / "golden-situational-60.jsonl"  # the live benchmark golden — single source of truth

load_dotenv(REPO_ROOT / ".env")


def gemini_api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        raise RuntimeError("GEMINI_API_KEY missing — put it in the repo-root .env")
    return key


def anthropic_api_key() -> str:
    """The cross-family eval judge (Claude Opus) key. Eval
    infra only — never an arm. Lives in the same gitignored repo-root .env."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY missing — put it in the repo-root .env")
    return key
