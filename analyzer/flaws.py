"""
Flaw reconciliation layer.

Every audit tool (Lighthouse, axe-core, pyseoanalyzer, extruct, textstat,
security headers, broken-link checker, ...) used to dump its raw output
straight into the AI prompt as a separate unranked text block, leaving the
LLM to figure out what mattered and to silently absorb redundant/contradictory
signals (e.g. Lighthouse's own accessibility score and a separate axe-core
scan both measuring accessibility, independently, never compared).

This module gives every tool one common, typed output — a Flaw — so they can
be deduplicated, ranked by severity, and handed to the AI as a single
pre-reconciled list instead of raw dumps. The AI's job shifts from "find the
flaws in this pile of raw data" to "write compelling copy about this ranked
list" — more consistent results, and it's obvious in code (not just in an
LLM's judgment call) which signal wins when two tools disagree.
"""

from dataclasses import dataclass

_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


@dataclass
class Flaw:
    category: str  # performance | seo | accessibility | security | content | tech
    severity: str  # critical | high | medium | low
    description: str


def rank(flaws: list[Flaw]) -> list[Flaw]:
    """Sort flaws most-severe first."""
    return sorted(flaws, key=lambda f: _SEVERITY_ORDER.get(f.severity, 4))
