"""Company context: SOPs (with machine-readable frontmatter), policies, rules.

SOPs are the company's own description of a task. The frontmatter also defines
what "done" means (success_checks), so verification criteria come from the
company, not from the agent.
"""
import re
from dataclasses import dataclass, field
from functools import lru_cache

import yaml
from rank_bm25 import BM25Okapi

from .config import COMPANY_DIR


@dataclass
class SOP:
    intent: str
    title: str
    body: str
    inputs: list = field(default_factory=list)
    success_checks: list = field(default_factory=list)
    kind: str = "action"   # "answer" = information request, no system change to verify


def _tok(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


@lru_cache
def load_sops() -> dict[str, SOP]:
    sops = {}
    for p in sorted((COMPANY_DIR / "sops").glob("*.md")):
        raw = p.read_text()
        meta, body = {}, raw
        if raw.startswith("---"):
            _, fm, body = raw.split("---", 2)
            meta = yaml.safe_load(fm) or {}
        sop = SOP(intent=meta["intent"], title=meta.get("title", p.stem), body=body.strip(),
                  inputs=meta.get("inputs", []), success_checks=meta.get("success_checks", []),
                  kind=meta.get("kind", "action"))
        sops[sop.intent] = sop
    return sops


@lru_cache
def _index():
    sops = list(load_sops().values())
    return sops, BM25Okapi([_tok(s.title + " " + s.body) for s in sops])


def search_sops(query: str, k: int = 3) -> list[SOP]:
    sops, bm25 = _index()
    scores = bm25.get_scores(_tok(query))
    ranked = sorted(zip(scores, sops), key=lambda x: -x[0])
    return [s for score, s in ranked[:k] if score > 0] or sops[:k]


def policies_text() -> str:
    return (COMPANY_DIR / "policies.md").read_text()


@lru_cache
def rules() -> list[dict]:
    return yaml.safe_load((COMPANY_DIR / "rules.yaml").read_text())["rules"]
