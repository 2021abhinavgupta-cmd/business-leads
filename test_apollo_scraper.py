"""
Unit tests for ApolloFreeScraper (scrapers/apollo_free.py), added 2026-09-16
after two real bugs surfaced from checking the scraper against Apollo's own
API docs rather than trusting the existing code:

1. The search URL was missing the /api segment (https://api.apollo.io/v1/...
   instead of https://api.apollo.io/api/v1/...) — would 404/fail on every
   real call.
2. The search endpoint never returns email/phone at all (confirmed in
   Apollo's docs), but the old code read person.get("email", "") straight
   off the search response — every Apollo lead's Email was silently always
   blank. Fixed by adding a real per-person enrichment call
   (POST /people/match with reveal_personal_emails=true).

The real Apollo API is never called — httpx.Client is monkeypatched.
"""

import httpx
import config
from scrapers.apollo_free import ApolloFreeScraper


def _response(status_code: int, json_data: dict):
    request = httpx.Request("POST", "https://api.apollo.io/x")
    return httpx.Response(status_code, json=json_data, request=request)


class _FakeClient:
    """Stands in for httpx.Client(timeout=...) as a context manager."""
    def __init__(self, post_fn):
        self._post_fn = post_fn

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, headers=None, json=None):
        return self._post_fn(url, headers, json)


def _scraper(monkeypatch, post_fn):
    monkeypatch.setattr(config, "APOLLO_API_KEY", "fake-key")
    monkeypatch.setattr("scrapers.apollo_free.httpx.Client", lambda timeout=None: _FakeClient(post_fn))
    return ApolloFreeScraper()


def _search_person(first_name="Jane", last_name="Doe", company="Acme", website="https://acme.com"):
    return {
        "first_name": first_name,
        "last_name": last_name,
        "organization": {"name": company, "website_url": website, "city": "Pune", "estimated_num_employees": 12},
    }


def test_returns_empty_list_without_an_api_key(monkeypatch):
    monkeypatch.setattr(config, "APOLLO_API_KEY", "")

    def _explode(*a, **k):
        raise AssertionError("should not touch the network without an API key")

    monkeypatch.setattr("scrapers.apollo_free.httpx.Client", lambda timeout=None: _FakeClient(_explode))
    assert ApolloFreeScraper().scrape("Dentist") == []


def test_search_hits_the_correct_api_v1_path(monkeypatch):
    """Regression test for the missing /api segment — the real bug that
    would have made every search 404."""
    called_urls = []

    def _post_fn(url, headers, json):
        called_urls.append(url)
        return _response(200, {"people": []})

    scraper = _scraper(monkeypatch, _post_fn)
    scraper.scrape("Dentist")

    assert called_urls[0] == "https://api.apollo.io/api/v1/mixed_people/api_search"


def test_search_payload_targets_small_indian_decision_makers(monkeypatch):
    """
    Pins the 2026-09-16 lead-quality narrowing ("good leads i can convert
    into clients") — regression test against silently drifting back to an
    unfiltered global keyword search.
    """
    captured = {}

    def _post_fn(url, headers, json):
        if "mixed_people" in url:
            captured.update(json)
        return _response(200, {"people": []})

    scraper = _scraper(monkeypatch, _post_fn)
    scraper.scrape("Dentist")

    assert captured["person_seniorities"] == ["owner", "founder", "c_suite"]
    assert captured["organization_num_employees_ranges"] == ["1,10", "11,50"]
    assert captured["organization_locations"] == ["india"]


def test_a_lead_with_no_company_or_website_is_skipped_without_enriching(monkeypatch):
    """No point spending an enrichment credit on a lead we're discarding."""
    match_calls = []

    def _post_fn(url, headers, json):
        if "mixed_people" in url:
            return _response(200, {"people": [
                {"first_name": "Jane", "last_name": "Doe", "organization": {"name": "", "website_url": ""}},
            ]})
        match_calls.append(url)
        return _response(200, {"person": {"email": "jane@acme.com"}})

    scraper = _scraper(monkeypatch, _post_fn)
    leads = scraper.scrape("Dentist")

    assert leads == []
    assert match_calls == []


def test_a_qualifying_lead_gets_enriched_with_a_real_email(monkeypatch):
    def _post_fn(url, headers, json):
        if "mixed_people" in url:
            return _response(200, {"people": [_search_person()]})
        assert url == "https://api.apollo.io/api/v1/people/match"
        assert json["first_name"] == "Jane"
        assert json["last_name"] == "Doe"
        assert json["organization_name"] == "Acme"
        assert json["reveal_personal_emails"] is True
        return _response(200, {"person": {"email": "jane@acme.com"}})

    scraper = _scraper(monkeypatch, _post_fn)
    leads = scraper.scrape("Dentist")

    assert len(leads) == 1
    assert leads[0]["Email"] == "jane@acme.com"
    assert leads[0]["Company"] == "Acme"
    assert leads[0]["Decision Maker Name"] == "Jane Doe"


def test_a_failed_enrichment_call_leaves_email_blank_without_crashing_the_batch(monkeypatch):
    def _post_fn(url, headers, json):
        if "mixed_people" in url:
            return _response(200, {"people": [_search_person(), _search_person(first_name="Bob", last_name="Lee")]})
        # Second person's enrichment call fails; the first (Jane) is fine.
        if json["first_name"] == "Bob":
            raise httpx.ConnectError("boom")
        return _response(200, {"person": {"email": "jane@acme.com"}})

    scraper = _scraper(monkeypatch, _post_fn)
    leads = scraper.scrape("Dentist")

    assert len(leads) == 2
    assert leads[0]["Email"] == "jane@acme.com"
    assert leads[1]["Email"] == ""


def test_enrichment_not_attempted_when_the_person_has_no_full_name(monkeypatch):
    match_calls = []

    def _post_fn(url, headers, json):
        if "mixed_people" in url:
            return _response(200, {"people": [_search_person(first_name="", last_name="")]})
        match_calls.append(url)
        return _response(200, {"person": {"email": "should-not-be-used@x.com"}})

    scraper = _scraper(monkeypatch, _post_fn)
    leads = scraper.scrape("Dentist")

    assert len(leads) == 1
    assert leads[0]["Email"] == ""
    assert match_calls == []


def test_a_match_with_no_email_on_file_returns_blank_not_an_error(monkeypatch):
    def _post_fn(url, headers, json):
        if "mixed_people" in url:
            return _response(200, {"people": [_search_person()]})
        return _response(200, {"person": {}})

    scraper = _scraper(monkeypatch, _post_fn)
    leads = scraper.scrape("Dentist")

    assert leads[0]["Email"] == ""


# ---------------------------------------------------------------
# City narrowing (added 2026-09-25)
# ---------------------------------------------------------------

def test_scrape_defaults_to_searching_all_of_india(monkeypatch):
    captured = {}

    def _post(url, headers, json):
        captured.update(json or {})
        return _response(200, {"people": []})

    _scraper(monkeypatch, _post).scrape("Dentist", limit=5)

    assert captured["organization_locations"] == ["india"]


def test_scrape_narrows_to_a_given_city(monkeypatch):
    captured = {}

    def _post(url, headers, json):
        captured.update(json or {})
        return _response(200, {"people": []})

    _scraper(monkeypatch, _post).scrape("Dentist", limit=5, city="Mumbai")

    # Lowercased to match the example values in Apollo's own docs
    # ("texas", "tokyo", "spain").
    assert captured["organization_locations"] == ["mumbai"]


def test_scrape_treats_a_whitespace_only_city_as_no_city(monkeypatch):
    captured = {}

    def _post(url, headers, json):
        captured.update(json or {})
        return _response(200, {"people": []})

    _scraper(monkeypatch, _post).scrape("Dentist", limit=5, city="   ")

    assert captured["organization_locations"] == ["india"]


# ---------------------------------------------------------------------------
# Refused requests raise instead of returning empty (added 2026-09-27)
#
# A live search reported "Added 0 leads from Apollo" and neither the user nor
# the logs could tell an invalid key from a plan restriction from a genuinely
# empty search — all three produced an identical empty list.
# ---------------------------------------------------------------------------

import pytest
from scrapers.apollo_free import ApolloError


def _raises_status(status, body=None):
    def _post(url, headers, json):
        return _response(status, body if body is not None else {"error": "nope"})
    return _post


def test_an_invalid_key_raises_with_a_fixable_message(monkeypatch):
    scraper = _scraper(monkeypatch, _raises_status(401))

    with pytest.raises(ApolloError) as excinfo:
        scraper.scrape("Dentist", limit=5)

    message = str(excinfo.value)
    assert "APOLLO_API_KEY" in message
    assert "Railway" in message


def test_a_plan_restriction_says_so_rather_than_blaming_the_key(monkeypatch):
    scraper = _scraper(monkeypatch, _raises_status(403))

    with pytest.raises(ApolloError) as excinfo:
        scraper.scrape("Dentist", limit=5)

    assert "plan" in str(excinfo.value).lower()


def test_an_unknown_status_still_raises_with_the_status_in_it(monkeypatch):
    scraper = _scraper(monkeypatch, _raises_status(500))

    with pytest.raises(ApolloError) as excinfo:
        scraper.scrape("Dentist", limit=5)

    assert "500" in str(excinfo.value)


def test_a_transport_failure_raises_rather_than_looking_like_no_results(monkeypatch):
    def _boom(url, headers, json):
        raise httpx.ConnectError("no network")

    scraper = _scraper(monkeypatch, _boom)

    with pytest.raises(ApolloError):
        scraper.scrape("Dentist", limit=5)


def test_an_empty_result_is_still_just_an_empty_list(monkeypatch):
    """The whole point: empty must now mean "Apollo answered, nobody matched"."""
    scraper = _scraper(monkeypatch, lambda url, headers, json: _response(200, {"people": []}))

    assert scraper.scrape("Dentist", limit=5) == []


def test_a_missing_key_still_returns_empty_rather_than_raising(monkeypatch):
    # The endpoint already 400s on a missing key before calling this, and the
    # scheduler treats "not configured" as "skip this source", so this stays
    # a quiet no-op rather than becoming an error.
    monkeypatch.setattr(config, "APOLLO_API_KEY", "")
    assert ApolloFreeScraper().scrape("Dentist", limit=5) == []


# ---------------------------------------------------------------------------
# Domain targeting — how a district search is actually served
# ---------------------------------------------------------------------------

def test_domains_are_sent_as_the_apollo_domain_list(monkeypatch):
    captured = {}

    def _post(url, headers, json):
        captured.update(json or {})
        return _response(200, {"people": []})

    _scraper(monkeypatch, _post).scrape("", limit=5, domains=["acme.com", "beta.in"])

    assert captured["q_organization_domains_list"] == ["acme.com", "beta.in"]


def test_domain_targeting_drops_the_location_filter(monkeypatch):
    """
    The caller picked these companies off a map. Layering Apollo's own
    geography guess on top could only remove correct answers.
    """
    captured = {}

    def _post(url, headers, json):
        captured.update(json or {})
        return _response(200, {"people": []})

    _scraper(monkeypatch, _post).scrape("", limit=5, city="Mumbai", domains=["acme.com"])

    assert "organization_locations" not in captured


def test_domain_targeting_drops_the_company_size_filter(monkeypatch):
    """
    The size filter exists to stop a blind keyword search dragging in
    enterprises. These companies were hand-picked from one district, so
    discarding one because Apollo thinks it has 60 staff would throw away a
    lead the user explicitly asked for.
    """
    captured = {}

    def _post(url, headers, json):
        captured.update(json or {})
        return _response(200, {"people": []})

    _scraper(monkeypatch, _post).scrape("", limit=5, domains=["acme.com"])

    assert "organization_num_employees_ranges" not in captured


def test_domain_targeting_keeps_a_seniority_filter(monkeypatch):
    """
    Seniority is the one narrowing filter a domain search keeps — reaching a
    decision maker is the point. The exact list was widened later the same
    day (see the Director/Partner tests at the end of this file) after a live
    search matched nobody; this only guards that the filter still exists at
    all and still demands an owner-level role.
    """
    captured = {}

    def _post(url, headers, json):
        captured.update(json or {})
        return _response(200, {"people": []})

    _scraper(monkeypatch, _post).scrape("", limit=5, domains=["acme.com"])

    assert "owner" in captured["person_seniorities"]
    assert "founder" in captured["person_seniorities"]


def test_a_blank_niche_sends_no_keyword_at_all_with_domains(monkeypatch):
    captured = {}

    def _post(url, headers, json):
        captured.update(json or {})
        return _response(200, {"people": []})

    _scraper(monkeypatch, _post).scrape("   ", limit=5, domains=["acme.com"])

    assert "q_keywords" not in captured


def test_a_niche_still_narrows_a_domain_search_when_given(monkeypatch):
    captured = {}

    def _post(url, headers, json):
        captured.update(json or {})
        return _response(200, {"people": []})

    _scraper(monkeypatch, _post).scrape("Dentist", limit=5, domains=["acme.com"])

    assert captured["q_keywords"] == "Dentist"


def test_the_domain_list_is_capped_at_apollos_limit(monkeypatch):
    captured = {}

    def _post(url, headers, json):
        captured.update(json or {})
        return _response(200, {"people": []})

    _scraper(monkeypatch, _post).scrape("", limit=5, domains=[f"d{i}.com" for i in range(1500)])

    assert len(captured["q_organization_domains_list"]) == 1000


def test_blank_domains_fall_back_to_the_ordinary_city_search(monkeypatch):
    captured = {}

    def _post(url, headers, json):
        captured.update(json or {})
        return _response(200, {"people": []})

    _scraper(monkeypatch, _post).scrape("Dentist", limit=5, city="Mumbai", domains=[])

    assert captured["organization_locations"] == ["mumbai"]
    assert "q_organization_domains_list" not in captured


# ---------------------------------------------------------------------------
# Widened reach on domain searches + the coverage probe (added 2026-09-27)
#
# A live BKC search found 60 companies and matched 0 people. Apollo combines
# person_titles and person_seniorities with AND (confirmed in the docs), so
# the old pairing demanded a literal title from {founder, ceo, owner, cmo,
# marketing} AND a seniority from {owner, founder, c_suite} — which discards
# the Director / Partner / Proprietor who actually runs an Indian SME.
# ---------------------------------------------------------------------------

def _capturing(monkeypatch, response=None):
    captured = {}

    def _post(url, headers, json):
        captured.update(json or {})
        return _response(200, response if response is not None else {"people": []})

    return _scraper(monkeypatch, _post), captured


def test_a_domain_search_sends_no_job_title_filter(monkeypatch):
    """
    Titles AND seniorities is the trap: a Director clears the seniority
    filter and then fails the title filter, so the pairing threw away every
    Indian SME decision maker.
    """
    scraper, captured = _capturing(monkeypatch)
    scraper.scrape("", limit=5, domains=["acme.com"])

    assert "person_titles" not in captured


def test_a_domain_search_includes_directors_and_partners(monkeypatch):
    scraper, captured = _capturing(monkeypatch)
    scraper.scrape("", limit=5, domains=["acme.com"])

    assert set(captured["person_seniorities"]) == {
        "owner", "founder", "c_suite", "partner", "director",
    }


def test_the_ordinary_city_search_keeps_its_original_filters(monkeypatch):
    """
    The widening is scoped to domain searches. A blind city keyword search
    still needs the tighter pairing, or it drags in any director anywhere.
    """
    scraper, captured = _capturing(monkeypatch)
    scraper.scrape("Dentist", limit=5, city="Mumbai")

    assert captured["person_titles"] == ["founder", "ceo", "owner", "cmo", "marketing"]
    assert captured["person_seniorities"] == ["owner", "founder", "c_suite"]


def test_the_coverage_probe_asks_with_no_role_filter_at_all(monkeypatch):
    scraper, captured = _capturing(monkeypatch, {"pagination": {"total_entries": 23}})

    assert scraper.count_people_at_domains(["acme.com", "beta.in"]) == 23
    assert captured["q_organization_domains_list"] == ["acme.com", "beta.in"]
    # The whole point is measuring coverage, so every narrowing filter has
    # to be absent or the number answers a different question.
    for narrowing in ("person_titles", "person_seniorities", "q_keywords",
                      "organization_locations", "organization_num_employees_ranges"):
        assert narrowing not in captured, narrowing


def test_the_coverage_probe_reports_zero_when_apollo_knows_nobody(monkeypatch):
    scraper, _ = _capturing(monkeypatch, {"pagination": {"total_entries": 0}, "people": []})

    assert scraper.count_people_at_domains(["acme.com"]) == 0


def test_the_coverage_probe_falls_back_to_counting_returned_people(monkeypatch):
    scraper, _ = _capturing(monkeypatch, {"people": [{"first_name": "A"}, {"first_name": "B"}]})

    assert scraper.count_people_at_domains(["acme.com"]) == 2


def test_the_coverage_probe_never_raises(monkeypatch):
    """
    It is a diagnostic bolted onto an already-empty result. It must never
    turn that into a failed request.
    """
    def _boom(url, headers, json):
        raise httpx.ConnectError("no network")

    scraper = _scraper(monkeypatch, _boom)

    assert scraper.count_people_at_domains(["acme.com"]) is None


def test_the_coverage_probe_answers_nothing_without_a_key_or_domains(monkeypatch):
    scraper, _ = _capturing(monkeypatch)
    assert scraper.count_people_at_domains([]) is None

    monkeypatch.setattr(config, "APOLLO_API_KEY", "")
    assert ApolloFreeScraper().count_people_at_domains(["acme.com"]) is None
