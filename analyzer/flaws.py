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
    category: str  # performance | seo | accessibility | security | content | tech | conversion
    severity: str  # critical | high | medium | low
    description: str


def rank(flaws: list[Flaw]) -> list[Flaw]:
    """Sort flaws most-severe first."""
    return sorted(flaws, key=lambda f: _SEVERITY_ORDER.get(f.severity, 4))


_SEVERITY_PENALTY = {"critical": 25, "high": 12, "medium": 5, "low": 2}

# Most one category can subtract from the score. Without a ceiling, a single
# noisy subsystem dominates the total: axe-core routinely reports 15+
# accessibility violations on an ordinary site, which alone would bottom the
# score out at 0 and make a site with one real problem indistinguishable
# from a site that is broken in every dimension. Since the score gates
# whether a lead is contacted at all, that flattening loses the signal the
# score exists to carry.
_MAX_PENALTY_PER_CATEGORY = 35


def compute_score(flaws: list[Flaw]) -> int:
    """
    Deterministic 0-100 health score computed directly from the real,
    measured flaw list — the same start-at-100, subtract-by-severity
    approach a human auditor would use manually. Used instead of trusting
    the AI's self-reported overall_score, which has no actual connection to
    the data it was given and can vary run-to-run on identical input, even
    though it's the single signal that gates whether a lead gets contacted
    at all (see analyzer/ai_audit.py:should_contact).

    Each category's total penalty is capped (see
    _MAX_PENALTY_PER_CATEGORY) so one noisy signal can't swamp the rest,
    and a site with problems spread across many areas scores worse than a
    site with many findings in a single area — which matches how a human
    would actually judge overall health.

    NOTE: fewer flaws produces a HIGHER score, so a partially-failed audit
    inflates this number. Callers must not read a high score as "healthy"
    without checking WebsiteData.signal_status — see
    analyzer/ai_audit.py:analyze_lead, which records partial coverage
    precisely so the skip-if-too-good path can't discard an unmeasured lead.
    """
    penalty_by_category: dict[str, int] = {}
    for flaw in flaws:
        penalty = _SEVERITY_PENALTY.get(flaw.severity, 0)
        penalty_by_category[flaw.category] = penalty_by_category.get(flaw.category, 0) + penalty

    total_penalty = sum(
        min(penalty, _MAX_PENALTY_PER_CATEGORY)
        for penalty in penalty_by_category.values()
    )
    return max(0, min(100, 100 - total_penalty))
