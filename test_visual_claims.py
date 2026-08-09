"""
Tests for the 2026-08-09 visual-claim accuracy pass.

The audit prompt used to MANDATE a design critique on every single email
("ONE OF YOUR FLAWS MUST BE A VISUAL CRITIQUE"), whether or not anything
visual had actually been measured. A site with clean design left the model
no honest way to comply, so it invented something — and the demand directly
contradicted the source_quote rule ("if you cannot copy an exact line for
this claim, do not make the claim at all"), since a typography or alignment
observation usually has no matching line in FLAWS DETECTED.

That also made the one mandated claim per email the one claim nothing could
verify: _verify_source_quotes has no line to match, and _verify_grounding
judges text against text with the image never in view.

Covered here: the evidence gate on the prompt, the vision judge that grounds
whatever visual claims do get made, and the _BOILERPLATE_NUMBERS narrowing.

Unit tests only — no network, no API keys, no real image.
"""

import pytest

from analyzer.ai_audit import AIAuditor
from scrapers.website import WebsiteData


def _real_web(flaws=(), visual_flaw_context=""):
    """
    A real WebsiteData — _build_prompt reads far more than the two fields the
    evidence check touches, so the prompt tests need the actual dataclass.
    """
    return WebsiteData(
        url="https://example.com", reachable=True, load_time_ms=1200,
        page_speed_score=55, seo_score=70, mobile_score=60,
        has_cta=True, has_contact=True, has_testimonials=True, has_blog=True,
        has_ssl=True, meta_title="Acme", meta_description="We do things",
        flaws=list(flaws), visual_flaw_context=visual_flaw_context,
    )


class _FakeFlaw:
    """Stands in for analyzer.flaws.Flaw — only .description is read here."""

    def __init__(self, description):
        self.description = description
        self.severity = "medium"
        self.category = "content"


class _FakeWeb:
    """Minimal WebsiteData stand-in for the visual-evidence check."""

    def __init__(self, flaws=(), visual_flaw_context=None):
        self.flaws = list(flaws)
        self.visual_flaw_context = visual_flaw_context


# ---------------------------------------------------------------------------
# _has_visual_evidence — what counts as having actually measured something
# ---------------------------------------------------------------------------

def test_red_box_counts_as_visual_evidence():
    web = _FakeWeb(visual_flaw_context="Red box drawn around a nav link with poor contrast")
    assert AIAuditor._has_visual_evidence(web)


def test_font_consistency_flaw_counts_as_visual_evidence():
    web = _FakeWeb(flaws=[_FakeFlaw(
        "Uses 5 different fonts on one page (Arial, Roboto) - inconsistent typography reads as unpolished."
    )])
    assert AIAuditor._has_visual_evidence(web)


def test_stretched_image_flaw_counts_as_visual_evidence():
    web = _FakeWeb(flaws=[_FakeFlaw(
        "3 images are displayed larger than their native resolution and will look blurry to visitors."
    )])
    assert AIAuditor._has_visual_evidence(web)


def test_non_visual_flaws_are_not_visual_evidence():
    """A slow site with clean design must not trigger the mandatory critique."""
    web = _FakeWeb(flaws=[
        _FakeFlaw("Largest Contentful Paint takes 4.2s to render."),
        _FakeFlaw("No favicon found - the browser tab shows a blank icon."),
    ])
    assert not AIAuditor._has_visual_evidence(web)


def test_no_flaws_at_all_is_not_visual_evidence():
    assert not AIAuditor._has_visual_evidence(_FakeWeb())


# ---------------------------------------------------------------------------
# The prompt gate itself
# ---------------------------------------------------------------------------

def test_prompt_demands_a_visual_critique_only_with_real_evidence():
    web = _real_web(flaws=[_FakeFlaw("Uses 5 different fonts on one page (Arial, Roboto).")])
    prompt = AIAuditor._build_prompt("Acme", None, web, has_image=True)
    assert "ONE OF YOUR FLAWS MUST BE A VISUAL CRITIQUE" in prompt


def test_prompt_forbids_inventing_a_visual_critique_without_evidence():
    web = _real_web(flaws=[_FakeFlaw("Largest Contentful Paint takes 4.2s to render.")])
    prompt = AIAuditor._build_prompt("Acme", None, web, has_image=True)
    assert "ONE OF YOUR FLAWS MUST BE A VISUAL CRITIQUE" not in prompt
    assert "Do NOT invent a visual criticism" in prompt


def test_no_visual_instruction_at_all_without_a_screenshot():
    web = _real_web(flaws=[_FakeFlaw("Uses 5 different fonts on one page (Arial, Roboto).")])
    prompt = AIAuditor._build_prompt("Acme", None, web, has_image=False)
    assert "VISUAL CRITIQUE" not in prompt
    assert "Do NOT invent a visual criticism" not in prompt


# ---------------------------------------------------------------------------
# _verify_visual_claims — the vision judge
# ---------------------------------------------------------------------------

def test_visual_claim_keywords_route_only_design_claims_to_the_judge():
    assert AIAuditor._looks_like_a_visual_claim("the fonts in your hero and nav do not match")
    assert AIAuditor._looks_like_a_visual_claim("I noticed in the screenshot your menu overlaps")
    assert AIAuditor._looks_like_a_visual_claim("your layout feels cluttered")
    assert not AIAuditor._looks_like_a_visual_claim("your homepage takes 4.2 seconds to load")


def test_visual_verification_skipped_without_a_screenshot():
    auditor = AIAuditor.__new__(AIAuditor)
    parsed = {"flaws": [{"paragraph": "your fonts clash badly"}]}
    assert auditor._verify_visual_claims(parsed, "Acme", None) is None


def test_visual_verification_skipped_when_no_claim_is_visual(monkeypatch):
    auditor = AIAuditor.__new__(AIAuditor)
    monkeypatch.setattr(AIAuditor, "_encode_image", staticmethod(lambda p: "fake"))
    parsed = {"flaws": [{"paragraph": "your homepage takes 4.2 seconds to load"}]}
    assert auditor._verify_visual_claims(parsed, "Acme", "shot.jpg") is None


def test_visual_claim_not_visible_in_the_image_is_flagged(monkeypatch, capsys):
    auditor = AIAuditor.__new__(AIAuditor)
    monkeypatch.setattr(AIAuditor, "_encode_image", staticmethod(lambda p: "fake"))
    monkeypatch.setattr(AIAuditor, "_call_vision_judge", lambda self, p, img: '{"unsupported": [1]}')

    parsed = {"flaws": [{"paragraph": "the fonts in your hero section clash badly"}]}
    warning = auditor._verify_visual_claims(parsed, "Acme", "shot.jpg")
    capsys.readouterr()
    assert warning is not None
    assert "#1" in warning


def test_visual_claim_confirmed_in_the_image_passes(monkeypatch, capsys):
    auditor = AIAuditor.__new__(AIAuditor)
    monkeypatch.setattr(AIAuditor, "_encode_image", staticmethod(lambda p: "fake"))
    monkeypatch.setattr(AIAuditor, "_call_vision_judge", lambda self, p, img: '{"unsupported": []}')

    parsed = {"flaws": [{"paragraph": "the fonts in your hero section clash badly"}]}
    assert auditor._verify_visual_claims(parsed, "Acme", "shot.jpg") is None


def test_visual_verification_degrades_silently_with_no_vision_provider(monkeypatch):
    """Same contract as the other checks: a bonus safety net, never a failure."""
    auditor = AIAuditor.__new__(AIAuditor)
    monkeypatch.setattr(AIAuditor, "_encode_image", staticmethod(lambda p: "fake"))
    monkeypatch.setattr(AIAuditor, "_call_vision_judge", lambda self, p, img: None)

    parsed = {"flaws": [{"paragraph": "your layout looks cluttered"}]}
    assert auditor._verify_visual_claims(parsed, "Acme", "shot.jpg") is None


def test_a_judge_exception_never_fails_the_audit(monkeypatch, capsys):
    auditor = AIAuditor.__new__(AIAuditor)
    monkeypatch.setattr(AIAuditor, "_encode_image", staticmethod(lambda p: "fake"))

    def _boom(self, p, img):
        raise RuntimeError("vision provider exploded")

    monkeypatch.setattr(AIAuditor, "_call_vision_judge", _boom)
    parsed = {"flaws": [{"paragraph": "your layout looks cluttered"}]}
    assert auditor._verify_visual_claims(parsed, "Acme", "shot.jpg") is None
    assert "skipped" in capsys.readouterr().out


def test_only_visual_claims_are_sent_to_the_vision_judge(monkeypatch, capsys):
    """
    A non-visual claim must never be routed to the image judge — it would be
    marked unsupported simply because a load time isn't visible in a
    screenshot, which would turn a real warning into constant noise.
    """
    auditor = AIAuditor.__new__(AIAuditor)
    monkeypatch.setattr(AIAuditor, "_encode_image", staticmethod(lambda p: "fake"))
    captured = {}

    def _fake_judge(self, judge_prompt, img):
        captured["prompt"] = judge_prompt
        return '{"unsupported": [2]}'

    monkeypatch.setattr(AIAuditor, "_call_vision_judge", _fake_judge)

    parsed = {"flaws": [
        {"paragraph": "your fonts clash in the hero"},          # index 1, visual
        {"paragraph": "your site takes 4.2 seconds to load"},   # index 2, NOT visual
    ]}
    warning = auditor._verify_visual_claims(parsed, "Acme", "shot.jpg")
    capsys.readouterr()

    assert "4.2 seconds" not in captured["prompt"]
    # Claim 2 was never judged, so a stray "2" from the judge must be ignored
    # rather than reported against a paragraph it never saw.
    assert warning is None


# ---------------------------------------------------------------------------
# _BOILERPLATE_NUMBERS narrowing
# ---------------------------------------------------------------------------

def test_small_invented_counts_are_no_longer_excused(capsys):
    """
    "1"/"2"/"3" used to be blanket-excluded as "list positions", but the
    exclusion only ever applies to a number absent from the whole prompt —
    which makes it a made-up count, exactly what this check exists to catch.
    """
    parsed = {"flaws": [{"paragraph": "You have 3 broken links costing you customers."}]}
    warning = AIAuditor._check_number_hallucination(parsed, "we found 7 broken links", "Acme")
    capsys.readouterr()
    assert warning is not None
    assert "3" in warning


def test_the_ten_minute_call_boilerplate_is_still_excused(capsys):
    parsed = {"flaws": [{"paragraph": "Worth a 10 minute call?"}]}
    warning = AIAuditor._check_number_hallucination(parsed, "no numbers in this prompt", "Acme")
    capsys.readouterr()
    assert warning is None


def test_a_number_actually_present_in_the_source_is_never_flagged(capsys):
    parsed = {"flaws": [{"paragraph": "You have 3 broken links costing you customers."}]}
    warning = AIAuditor._check_number_hallucination(parsed, "we found 3 broken links", "Acme")
    capsys.readouterr()
    assert warning is None
