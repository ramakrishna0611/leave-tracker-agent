"""Per-company policy lookup: BM25 over the sections of that company's policy document only.

Each company sees only its own policy (the file is chosen by the logged-in user's company code).
"""
import re
from functools import lru_cache

from rank_bm25 import BM25Okapi

from leave_tracker.config import ROOT

POLICY_DIR = ROOT / "data" / "policies"


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


@lru_cache(maxsize=None)
def _index(org_code: str):
    path = POLICY_DIR / f"{org_code}.md"
    if not path.exists():
        return [], None
    sections = []
    for block in re.split(r"\n(?=## )", path.read_text(encoding="utf-8")):
        lines = block.strip().splitlines()
        if lines and lines[0].startswith("## "):
            sections.append({"title": lines[0][3:].strip(), "text": " ".join(lines[1:]).strip()})
    bm25 = BM25Okapi([_tokens(s["title"] + " " + s["text"]) for s in sections]) if sections else None
    return sections, bm25


def lookup(org_code: str, question: str, k: int = 2) -> list[dict]:
    sections, bm25 = _index(org_code)
    if not bm25:
        return []
    scores = bm25.get_scores(_tokens(question))
    best = sorted(range(len(sections)), key=lambda i: scores[i], reverse=True)[:k]
    return [sections[i] for i in best if scores[i] > 0]
