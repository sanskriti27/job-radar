import json
from datetime import datetime, timedelta, timezone

import pytest

from radar import filters, http, main, notify, scorer, sources
from radar.filters import TitleFilter, location_verdict
from radar.models import Job

import yaml
from pathlib import Path

FILT = yaml.safe_load((Path(__file__).parent.parent / "config" / "filters.yaml").read_text())
TF = TitleFilter(FILT["title_include"], FILT["title_exclude"])
NOW = datetime.now(timezone.utc)


# ---------- titles ----------

@pytest.mark.parametrize("title,ok", [
    ("Software Engineer II", True),
    ("SDE 2 - Payments", True),
    ("Backend Engineer (Java)", True),
    ("Full Stack Developer", True),
    ("Senior Software Engineer", True),          # left to Gemini's experience check
    ("Member of Technical Staff", True),
    ("Founding Engineer", True),
    ("Staff Software Engineer", False),
    ("Principal Engineer, Backend", False),
    ("Software Engineering Manager", False),
    ("SDE III", False),
    ("SDE-3, Payments", False),
    ("Software Engineer III, Google Cloud", True),   # Google L4 / Walmart SE III ≈ 2–4 yrs
    ("Software Engineer IV", False),
    ("Software Engineer 5", False),
    ("Frontend Engineer", False),
    ("Software Engineer in Test", False),
    ("Software Engineering Intern", False),
    ("iOS Developer", False),
    ("Tech Lead - Platform", False),
    ("Solutions Engineer", False),
    ("Account Executive", False),
])
def test_titles(title, ok):
    assert TF.ok(title) is ok


# ---------- locations ----------

def J(loc, remote=None, codes=None, desc=""):
    return Job(source="t", company="c", job_id="1", title="SWE", location=loc, remote=remote,
               country_codes=codes or [], description=desc)


@pytest.mark.parametrize("job,verdict", [
    (J("Hyderabad, Telangana, India"), "yes"),
    (J("Bengaluru"), "yes"),
    (J("Hyderabad, TS, IND"), "yes"),
    (J("Gurugram | London"), "yes"),
    (J("Indianapolis, IN"), "no"),
    (J("San Francisco, CA"), "no"),
    (J("Remote"), "maybe"),
    (J("Remote - APAC"), "yes"),
    (J("Remote, Worldwide"), "yes"),
    (J("Remote - US"), "maybe_foreign"),
    (J("London", codes=["IN"]), "yes"),
    (J("3 Locations"), "unknown"),
])
def test_locations(job, verdict):
    assert location_verdict(job) == verdict


# ---------- parsers ----------

def test_greenhouse(monkeypatch):
    monkeypatch.setattr(http, "get_json", lambda url, params=None, timeout=0: {"jobs": [
        {"id": 123, "title": " Software Engineer II ", "location": {"name": "Bengaluru, India"},
         "absolute_url": "https://x/123", "updated_at": "2026-11-12T05:00:00-04:00",
         "first_published": "2026-11-12T04:00:00-04:00"}]})
    [j] = sources.fetch_greenhouse("Acme", "acme")
    assert j.title == "Software Engineer II" and j.uid == "greenhouse:acme:123"
    assert j.posted_at == datetime(2026, 11, 12, 8, 0, tzinfo=timezone.utc)


def test_lever(monkeypatch):
    monkeypatch.setattr(http, "get_json", lambda url, params=None, timeout=0: [
        {"id": "abc", "text": "Backend Engineer", "categories": {"location": "Remote", "allLocations": ["Remote", "Bangalore"]},
         "hostedUrl": "https://jobs.lever.co/acme/abc", "createdAt": 1762920000000, "workplaceType": "remote",
         "country": "in", "descriptionPlain": "Kafka", "lists": [{"text": "Req", "content": "<li>Java</li>"}]}])
    [j] = sources.fetch_lever("Acme", "acme")
    assert j.remote is True and "Bangalore" in j.location and j.country_codes == ["IN"] and "Java" in j.description


def test_ashby(monkeypatch):
    monkeypatch.setattr(http, "get_json", lambda url, params=None, timeout=0: {"jobs": [
        {"id": "u1", "title": "SWE", "location": "Remote", "secondaryLocations": [{"location": "Hyderabad"}],
         "isRemote": True, "jobUrl": "https://jobs.ashbyhq.com/acme/u1", "publishedAt": "2026-11-12T06:00:00.000+00:00",
         "address": {"postalAddress": {"addressCountry": "India"}}, "isListed": True},
        {"id": "u2", "title": "Hidden", "isListed": False}]})
    jobs = sources.fetch_ashby("Acme", "acme")
    assert len(jobs) == 1 and jobs[0].country_codes == ["India"] and "Hyderabad" in jobs[0].location


def test_workday_posted():
    assert sources.parse_workday_posted("Posted Today", NOW) == NOW
    assert sources.parse_workday_posted("Posted 3 Days Ago", NOW) == NOW - timedelta(days=3)
    assert sources.parse_workday_posted("Posted 30+ Days Ago", NOW) == NOW - timedelta(days=31)


def test_amazon(monkeypatch):
    monkeypatch.setattr(http, "get_json", lambda url, params=None, timeout=0: {"jobs": [
        {"id_icims": "2812345", "title": "Software Development Engineer II", "normalized_location": "Hyderabad, TS, IND",
         "posted_date": "November 11, 2026", "job_path": "/en/jobs/2812345/sde-ii", "country_code": "IND",
         "basic_qualifications": "- 2+ years"}]})
    [j] = sources.fetch_amazon()
    assert j.url == "https://www.amazon.jobs/en/jobs/2812345/sde-ii" and location_verdict(j) == "yes"


def test_html_to_text():
    assert sources.html_to_text("&lt;p&gt;Hi &amp;amp; bye&lt;/p&gt;") == "Hi & bye"


def test_slug_candidates():
    assert sources.slug_candidates("Abnormal Security") == ["abnormalsecurity", "abnormal-security"]
    assert "sarvam" in sources.slug_candidates("Sarvam AI")


# ---------- end-to-end run ----------

@pytest.fixture
def env(tmp_path, monkeypatch):
    board = {"jobs": []}
    monkeypatch.setattr(main, "STATE_PATH", tmp_path / "seen.json")
    monkeypatch.setattr(main, "THROTTLE_PATH", tmp_path / "throttle.json")
    monkeypatch.setattr(main, "load_config", lambda: (
        {"pinned": [{"name": "Acme", "ats": "fake"}], "auto": []}, FILT, "profile"))
    monkeypatch.setitem(sources.FETCHERS, "fake", lambda company, **_: list(board["jobs"]))
    sent = []
    monkeypatch.setattr(notify, "alert", lambda j, s: sent.append((j, s)))
    monkeypatch.setattr(notify, "summary", lambda t, l: sent.append(("summary", t)))
    monkeypatch.setattr(scorer, "score", lambda jobs, profile: {})
    return board, sent


def mk(i, title="Software Engineer II", loc="Hyderabad, India", age_h=0.2):
    return Job(source="fake", company="Acme", job_id=str(i), title=title, location=loc,
               url=f"https://x/{i}", posted_at=NOW - timedelta(hours=age_h), description="Java")


def test_first_run_is_silent_then_alerts(env):
    board, sent = env
    board["jobs"] = [mk(1), mk(2)]
    main.cmd_run()
    assert sent == []                                  # seeded silently
    board["jobs"] = [mk(1), mk(2), mk(3), mk(4, title="Staff Engineer"), mk(5, loc="Austin, TX"), mk(6, age_h=200)]
    main.cmd_run()
    assert [j.job_id for j, _ in sent] == ["3"]        # only the fresh, relevant, Indian one
    main.cmd_run()
    assert len(sent) == 1                              # never alerts twice


def test_flood_guard(env):
    board, sent = env
    board["jobs"] = [mk(1)]
    main.cmd_run()
    board["jobs"] = [mk(i) for i in range(100, 140)]   # 40 "new" ids at once = board changed format
    main.cmd_run()
    assert sent == []


def test_gemini_threshold(env, monkeypatch):
    board, sent = env
    board["jobs"] = [mk(1)]
    main.cmd_run()
    board["jobs"] = [mk(1), mk(2), mk(3), mk(4)]
    monkeypatch.setattr(scorer, "score", lambda jobs, profile: {
        "fake:acme:2": {"uid": "fake:acme:2", "score": 9, "location_ok": True},
        "fake:acme:3": {"uid": "fake:acme:3", "score": 4, "location_ok": True},
        "fake:acme:4": {"uid": "fake:acme:4", "score": 8, "location_ok": False}})
    main.cmd_run()
    assert [j.job_id for j, _ in sent] == ["2"]


def test_format():
    t, m = notify.format_job(mk(1), {"score": 9, "years": "2-4 yrs", "why": "Java + Kafka"})
    assert t.startswith("🔥 Acme") and "9/10" in m and "min ago" in m


# ---------- fixes from the first live validate ----------

def test_rippling(monkeypatch):
    monkeypatch.setattr(http, "get_json", lambda url, params=None, timeout=0: {"items": [
        {"id": "5eb7", "name": "Software Engineer II", "workLocations": ["Bangalore, India"],
         "url": "https://ats.rippling.com/rippling/jobs/5eb7"}], "totalPages": 1})
    [j] = sources.fetch_rippling("Rippling", "rippling")
    assert j.uid == "rippling:rippling:5eb7" and location_verdict(j) == "yes"


def test_discovery_error_is_not_cached_as_not_found(monkeypatch):
    import requests
    def boom(company, slug, **_):
        r = requests.Response(); r.status_code = 429
        raise requests.HTTPError(response=r)
    for ats in sources.DISCOVERABLE:
        monkeypatch.setitem(sources.FETCHERS, ats, boom)
    with pytest.raises(sources.DiscoveryError):
        sources.discover("PhonePe")


def test_discovery_404_everywhere_is_not_found(monkeypatch):
    def nf(company, slug, **_):
        raise http.NotFound(slug)
    for ats in sources.DISCOVERABLE:
        monkeypatch.setitem(sources.FETCHERS, ats, nf)
    assert sources.discover("Nobody Inc") is None


def test_old_not_found_entries_are_rechecked(tmp_path):
    from radar.state import State
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"seen": {}, "seeded": [], "discovery": {
        "PhonePe": {"ats": None, "checked": "2026-11-13"},
        "Stripe": {"ats": "greenhouse", "slug": "stripe", "count": 785, "checked": "2026-11-13"}}}))
    s = State(p)
    assert s.discovery("PhonePe") is None and s.discovery("Stripe")["slug"] == "stripe"


def test_workday_422_falls_back(monkeypatch):
    import requests
    calls = []
    def post(url, payload, headers=None, timeout=0):
        calls.append(payload["searchText"])
        if payload["searchText"] == "software engineer india":
            r = requests.Response(); r.status_code = 422
            raise requests.HTTPError(response=r)
        return {"jobPostings": [{"title": "Software Engineer III", "externalPath": "/job/x/R1",
                                 "locationsText": "Bangalore, India", "postedOn": "Posted Today", "bulletFields": ["R1"]}]}
    monkeypatch.setattr(http, "post_json", post)
    jobs = sources.fetch_workday("Walmart", host="walmart.wd5.myworkdayjobs.com", tenant="walmart",
                                 site="WalmartExternal", search="software engineer india", pages=1)
    assert calls == ["software engineer india", "software engineer"] and jobs[0].job_id == "R1"


# ---------- fixes from the 16 Nov review ----------

def test_empty_gemini_model_variable_uses_default(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "")
    monkeypatch.setattr(scorer, "_working_model", None)
    assert scorer._models()[0] == "" and [m for m in scorer._models() if m][0] == "gemini-flash-latest"
    urls = []
    def post(url, payload, headers=None, timeout=0):
        urls.append(url)
        return {"candidates": [{"content": {"parts": [{"text": '[{"uid":"fake:acme:1","score":8}]'}]}}]}
    monkeypatch.setattr(http, "post_json", post)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    out = scorer.score([mk(1)], "p")
    assert "/models/gemini-flash-latest:generateContent" in urls[0] and out["fake:acme:1"]["score"] == 8


def test_gemini_falls_back_when_model_retired(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "gone-model")
    monkeypatch.setattr(scorer, "_working_model", None)
    def post(url, payload, headers=None, timeout=0):
        if "gone-model" in url:
            raise http.NotFound(url)
        return {"candidates": [{"content": {"parts": [{"text": "[]"}]}}]}
    monkeypatch.setattr(http, "post_json", post)
    assert scorer._generate({}, "k") == "[]" and scorer._working_model == "gemini-flash-latest"


def test_greenhouse_html_fallback():
    page = '''<a href="https://job-boards.greenhouse.io/phonepe/jobs/7690461003"><p class="t">Software Engineer</p>
              <span>New</span><p class="l">Bengaluru</p></a>
              <a href="/phonepe/jobs/7690461004"><p>SDE 2 &amp; Backend</p><p>Pune</p></a>'''
    a, b = sources.parse_greenhouse_html("PhonePe", "phonepe", page)
    assert (a.title, a.location, a.job_id) == ("Software Engineer", "Bengaluru", "7690461003")
    assert b.title == "SDE 2 & Backend" and b.url == "https://job-boards.greenhouse.io/phonepe/jobs/7690461004"


def test_workable_and_recruitee(monkeypatch):
    monkeypatch.setattr(http, "get_json", lambda url, params=None, timeout=0: {"jobs": [
        {"shortcode": "AB12", "title": "Backend Engineer", "city": "Bengaluru", "country": "India",
         "url": "https://apply.workable.com/j/AB12", "published_on": "2026-11-16", "telecommuting": False}]})
    [j] = sources.fetch_workable("Acme", "acme")
    assert location_verdict(j) == "yes" and j.posted_at is not None
    monkeypatch.setattr(http, "get_json", lambda url, params=None, timeout=0: {"offers": [
        {"id": 9, "title": "SDE II", "location": "Hyderabad, India", "country_code": "in",
         "careers_url": "https://acme.recruitee.com/o/sde", "published_at": "2026-11-16 06:00:00 UTC"}]})
    [j] = sources.fetch_recruitee("Acme", "acme")
    assert j.country_codes == ["IN"] and j.posted_at.hour == 6


def test_throttle_skips_recently_polled_workday(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "THROTTLE_PATH", tmp_path / "t.json")
    srcs = [{"name": "Adobe", "ats": "workday"}, {"name": "Stripe", "ats": "greenhouse", "slug": "stripe"}]
    assert len(main.due_sources(srcs)) == 2
    assert [s["name"] for s in main.due_sources(srcs)] == ["Stripe"]


def test_gemini_overloaded_tries_next_model_and_senior_not_alerted_unscored(env, monkeypatch):
    import requests
    monkeypatch.setattr(scorer, "_working_model", None)
    monkeypatch.setenv("GEMINI_MODEL", "")
    def post(url, payload, headers=None, timeout=0):
        if "gemini-flash-latest" in url:
            r = requests.Response(); r.status_code = 503
            raise requests.HTTPError(response=r)
        return {"candidates": [{"content": {"parts": [{"text": "[]"}]}}]}
    monkeypatch.setattr(http, "post_json", post)
    assert scorer._generate({}, "k") == "[]" and scorer._working_model == "gemini-flash-lite-latest"
    board, sent = env
    board["jobs"] = [mk(1)]
    main.cmd_run()
    board["jobs"] = [mk(1), mk(2, title="Senior Software Engineer"), mk(3)]
    main.cmd_run()                      # env stubs scoring to return nothing = Gemini down
    assert [j.job_id for j, _ in sent] == ["3"]


def test_atlassian_and_oracle(monkeypatch):
    monkeypatch.setattr(http, "get_json", lambda url, params=None, timeout=0: [
        {"id": 27339, "title": "Software Engineer II", "locations": ["Bengaluru - India - Bengaluru", "Remote - India - Remote"],
         "portalJobPost": {"portalUrl": "https://careers-apac-atlassian.icims.com/jobs/27339/x/job", "id": 27339},
         "overview": "<p>Java</p>"}])
    [j] = sources.fetch_atlassian()
    assert location_verdict(j) == "yes" and j.url.endswith("/27339/x/job") and j.description == "Java"
    monkeypatch.setattr(http, "get_json", lambda url, params=None, timeout=0: {"items": [{"requisitionList": [
        {"Id": "154321", "Title": "Software Engineer II", "PostedDate": "2026-11-16", "PrimaryLocation": "Bangalore, Karnataka, India",
         "PrimaryLocationCountry": "IN", "secondaryLocations": [{"Name": "Hyderabad, India", "CountryCode": "IN"}]}]}]})
    [j] = sources.fetch_oracle("Uber", host="h", site="UberCareers")
    assert j.url == "https://h/hcmUI/CandidateExperience/en/sites/UberCareers/job/154321" and "Hyderabad" in j.location
