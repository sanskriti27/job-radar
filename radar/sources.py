"""Adapters for each careers system. Each `fetch_*` returns a list of Job; `enrich` fills in the description."""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from . import http
from .models import Job

log = logging.getLogger("radar.sources")

UTC = timezone.utc


# ---------- helpers ----------

def html_to_text(s: str | None) -> str:
    if not s:
        return ""
    s = html.unescape(s)            # Greenhouse double-escapes content
    s = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n", s).strip()


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    except ValueError:
        return None


def parse_epoch(v) -> datetime | None:
    if v in (None, ""):
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if v > 1e12:  # milliseconds
        v /= 1000
    return datetime.fromtimestamp(v, tz=UTC)


def parse_workday_posted(s: str | None, now: datetime | None = None) -> datetime | None:
    if not s:
        return None
    now = now or datetime.now(UTC)
    t = s.lower()
    if "today" in t or "just" in t:
        return now
    if "yesterday" in t:
        return now - timedelta(days=1)
    m = re.search(r"(\d+)\+?\s*day", t)
    if m:
        days = int(m.group(1)) + (1 if "+" in t else 0)
        return now - timedelta(days=days)
    return None


# ---------- Greenhouse ----------

def fetch_greenhouse(company: str, slug: str, **_) -> list[Job]:
    try:
        data = http.get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    except http.NotFound:
        jobs = _greenhouse_html(company, slug)   # some boards are public as pages but not through the API
        if not jobs:
            raise
        return jobs
    jobs = []
    for j in data.get("jobs", []):
        jobs.append(Job(
            source="greenhouse", company=company, job_id=str(j["id"]),
            title=j.get("title", "").strip(),
            location=(j.get("location") or {}).get("name", ""),
            url=j.get("absolute_url", ""),
            posted_at=parse_iso(j.get("first_published") or j.get("updated_at")),
            extra={"slug": slug},
        ))
    return jobs


_GH_ANCHOR = re.compile(r'<a[^>]+href="([^"]*?/jobs/(\d+)[^"]*)"[^>]*>(.*?)</a>', re.S)


def parse_greenhouse_html(company: str, slug: str, page_html: str) -> list[Job]:
    jobs, seen = [], set()
    for href, jid, inner in _GH_ANCHOR.findall(page_html):
        if jid in seen:
            continue
        seen.add(jid)
        parts = [html.unescape(t).strip() for t in re.split(r"<[^>]+>", inner)]
        parts = [t for t in parts if t and t.lower() != "new"]
        if not parts:
            continue
        url = href if href.startswith("http") else "https://job-boards.greenhouse.io" + href
        jobs.append(Job(source="greenhouse", company=company, job_id=jid, title=parts[0],
                        location=parts[-1] if len(parts) > 1 else "", url=url,
                        extra={"slug": slug, "html": True}))
    return jobs


def _greenhouse_html(company: str, slug: str) -> list[Job]:
    jobs, ids = [], set()
    for page in range(1, 11):
        r = http.get(f"https://job-boards.greenhouse.io/{slug}", params={"page": page})
        new = [j for j in parse_greenhouse_html(company, slug, r.text) if j.job_id not in ids]
        if not new:
            break
        ids.update(j.job_id for j in new)
        jobs.extend(new)
    return jobs


def _enrich_greenhouse(job: Job) -> None:
    if job.extra.get("html"):
        r = http.get(job.url)
        job.description = html_to_text(re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", r.text, flags=re.S))[:6000]
        return
    d = http.get_json(f"https://boards-api.greenhouse.io/v1/boards/{job.extra['slug']}/jobs/{job.job_id}")
    job.description = html_to_text(d.get("content"))
    offices = [o.get("name", "") for o in d.get("offices", []) if o.get("name")]
    if offices and offices[0] not in job.location:
        job.location = " | ".join([job.location] + offices) if job.location else " | ".join(offices)


# ---------- Lever ----------

def fetch_lever(company: str, slug: str, **_) -> list[Job]:
    data = http.get_json(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
    jobs = []
    for j in data if isinstance(data, list) else []:
        cats = j.get("categories") or {}
        locs = cats.get("allLocations") or ([cats["location"]] if cats.get("location") else [])
        wt = (j.get("workplaceType") or "").lower()
        jobs.append(Job(
            source="lever", company=company, job_id=str(j["id"]),
            title=(j.get("text") or "").strip(),
            location=" | ".join(locs),
            url=j.get("hostedUrl", ""),
            remote=True if wt == "remote" else (False if wt in ("onsite", "on-site", "hybrid") else None),
            posted_at=parse_epoch(j.get("createdAt")),
            description=(j.get("descriptionPlain") or "") + "\n" + " ".join(
                (l.get("text") or "") + ": " + html_to_text(l.get("content")) for l in j.get("lists") or []),
            country_codes=[j["country"].upper()] if j.get("country") else [],
        ))
    return jobs


# ---------- Ashby ----------

def fetch_ashby(company: str, slug: str, **_) -> list[Job]:
    data = http.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    jobs = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location") or ""] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        countries = []
        for addr in [j.get("address")] + [s.get("address") for s in j.get("secondaryLocations") or []]:
            pa = (addr or {}).get("postalAddress", addr or {}) or {}
            if pa.get("addressCountry"):
                countries.append(pa["addressCountry"])
        url = j.get("jobUrl") or j.get("applyUrl") or ""
        jobs.append(Job(
            source="ashby", company=company, job_id=str(j.get("id") or url),
            title=(j.get("title") or "").strip(),
            location=" | ".join(l for l in locs if l),
            url=url,
            remote=bool(j.get("isRemote")) or (j.get("workplaceType") or "").lower() == "remote",
            posted_at=parse_iso(j.get("publishedAt")),
            description=j.get("descriptionPlain") or "",
            country_codes=countries,
        ))
    return jobs


# ---------- SmartRecruiters ----------

def fetch_smartrecruiters(company: str, slug: str, **_) -> list[Job]:
    jobs, offset = [], 0
    while offset < 500:
        data = http.get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
                             params={"country": "in", "limit": 100, "offset": offset})
        content = data.get("content", [])
        for j in content:
            loc = j.get("location") or {}
            ident = (j.get("company") or {}).get("identifier") or slug
            jobs.append(Job(
                source="smartrecruiters", company=company, job_id=str(j["id"]),
                title=(j.get("name") or "").strip(),
                location=loc.get("fullLocation") or ", ".join(
                    x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x),
                url=f"https://jobs.smartrecruiters.com/{ident}/{j['id']}",
                remote=loc.get("remote"),
                posted_at=parse_iso(j.get("releasedDate")),
                country_codes=[loc["country"].upper()] if loc.get("country") else [],
                extra={"ref": j.get("ref")},
            ))
        if len(content) < 100:
            break
        offset += 100
    return jobs


def _enrich_smartrecruiters(job: Job) -> None:
    if not job.extra.get("ref"):
        return
    d = http.get_json(job.extra["ref"])
    secs = ((d.get("jobAd") or {}).get("sections") or {})
    job.description = "\n".join(html_to_text((secs.get(k) or {}).get("text"))
                                for k in ("jobDescription", "qualifications", "additionalInformation"))


# ---------- Workday ----------

def fetch_workday(company: str, host: str, tenant: str, site: str, search: str = "software engineer",
                  pages: int = 2, **_) -> list[Job]:
    base = f"https://{host}/wday/cxs/{tenant}/{site}"
    jobs = []
    for page in range(pages):
        data = None
        # Some Workday sites reject certain search strings with 422; fall back to simpler searches.
        for text in dict.fromkeys([search, re.sub(r"\s*india\s*", " ", search, flags=re.I).strip(), ""]):
            try:
                data = http.post_json(f"{base}/jobs", {"appliedFacets": {}, "limit": 20, "offset": page * 20,
                                                       "searchText": text},
                                      headers={"Accept": "application/json", "Origin": f"https://{host}",
                                               "Referer": f"https://{host}/en-US/{site}"})
                search = text
                break
            except Exception as e:
                if getattr(getattr(e, "response", None), "status_code", None) != 422 or text == "":
                    raise
        postings = (data or {}).get("jobPostings", [])
        for j in postings:
            path = j.get("externalPath", "")
            jobs.append(Job(
                source="workday", company=company,
                job_id=(j.get("bulletFields") or [path])[0] or path,
                title=(j.get("title") or "").strip(),
                location=j.get("locationsText", ""),
                url=f"https://{host}/en-US/{site}{path}",
                posted_at=parse_workday_posted(j.get("postedOn")),
                extra={"detail": f"{base}{path}"},
            ))
        if len(postings) < 20:
            break
    return jobs


def _enrich_workday(job: Job) -> None:
    d = http.get_json(job.extra["detail"])
    info = d.get("jobPostingInfo") or {}
    job.description = html_to_text(info.get("jobDescription"))
    locs = [info.get("location")] + list(info.get("additionalLocations") or [])
    locs = [l for l in locs if l]
    if locs:
        job.location = " | ".join(locs)
    if info.get("country", {}).get("descriptor"):
        job.country_codes.append(info["country"]["descriptor"])
    if info.get("remoteType"):
        job.remote = "remote" in str(info["remoteType"]).lower()


# ---------- Amazon ----------

def fetch_amazon(company: str = "Amazon", search: str = "software development engineer", **_) -> list[Job]:
    data = http.get_json("https://www.amazon.jobs/en/search.json", params={
        "base_query": search, "loc_query": "India", "country": "IND",
        "sort": "recent", "result_limit": 100, "offset": 0})
    jobs = []
    for j in data.get("jobs", []):
        posted = None
        if j.get("posted_date"):
            try:
                posted = datetime.strptime(j["posted_date"], "%B %d, %Y").replace(tzinfo=UTC)
            except ValueError:
                pass
        jobs.append(Job(
            source="amazon", company=company, job_id=str(j.get("id_icims") or j.get("id")),
            title=(j.get("title") or "").strip(),
            location=j.get("normalized_location") or j.get("location") or "",
            url="https://www.amazon.jobs" + (j.get("job_path") or ""),
            posted_at=posted,
            description="\n".join(html_to_text(j.get(k)) for k in
                                  ("description_short", "basic_qualifications", "preferred_qualifications")),
            country_codes=["IN"] if (j.get("country_code") or "").upper() in ("IND", "IN") else [],
        ))
    return jobs


# ---------- Microsoft (Eightfold "pcsx" search; experimental) ----------

def fetch_microsoft(company: str = "Microsoft", search: str = "software engineer", **_) -> list[Job]:
    data = http.get_json("https://apply.careers.microsoft.com/api/pcsx/search", params={
        "domain": "microsoft.com", "query": search, "location": "India", "start": 0, "sort_by": "timestamp"})
    root = data.get("data", data) if isinstance(data, dict) else {}
    positions = root.get("positions") or root.get("jobs") or []
    jobs = []
    for p in positions:
        pid = str(p.get("id") or p.get("jobId") or p.get("displayJobId"))
        locs = p.get("locations") or p.get("standardizedLocations") or []
        if isinstance(locs, str):
            locs = [locs]
        url = p.get("positionUrl") or p.get("canonicalPositionUrl") or f"/careers/job/{pid}"
        if url.startswith("/"):
            url = "https://apply.careers.microsoft.com" + url
        jobs.append(Job(
            source="microsoft", company=company, job_id=pid,
            title=(p.get("name") or p.get("title") or "").strip(),
            location=" | ".join(str(l) for l in locs) or p.get("location", ""),
            url=url,
            remote=("remote" in str(p.get("workLocationOption", "")).lower()) or None,
            posted_at=parse_epoch(p.get("postedTs") or p.get("creationTs") or p.get("t_create")),
            description=html_to_text(p.get("jobDescription") or p.get("description")),
        ))
    return jobs


# ---------- Google (HTML results page; experimental) ----------

def fetch_google(company: str = "Google", search: str = "software engineer", **_) -> list[Job]:
    r = http.get("https://www.google.com/about/careers/applications/jobs/results",
                 params={"location": "India", "q": search, "sort_by": "date"})
    jobs, seen = [], set()
    for m in re.finditer(r'jobs/results/(\d{6,})-([a-z0-9-]+)', r.text):
        jid, slug = m.group(1), m.group(2)
        if jid in seen:
            continue
        seen.add(jid)
        jobs.append(Job(
            source="google", company=company, job_id=jid,
            title=slug.replace("-", " ").title(),
            location="India",  # search was restricted to India
            url=f"https://www.google.com/about/careers/applications/jobs/results/{jid}-{slug}",
            country_codes=["IN"],
        ))
    return jobs


# ---------- Rippling ATS (ats.rippling.com; used by Rippling and many startups) ----------

def fetch_rippling(company: str, slug: str, **_) -> list[Job]:
    jobs, page = [], 0
    while page < 20:
        data = http.get_json(f"https://ats.rippling.com/api/v2/board/{slug}/jobs", params={"page": page, "pageSize": 100})
        items = data if isinstance(data, list) else (
            data.get("items") or data.get("results") or data.get("jobs") or data.get("data") or [])
        for j in items:
            jid = str(j.get("id") or j.get("uuid") or "")
            if not jid:
                continue
            locs = j.get("workLocations") or j.get("locations") or []
            if isinstance(locs, dict):
                locs = [locs]
            locs = [l.get("name") or l.get("label") or "" if isinstance(l, dict) else str(l) for l in locs]
            jobs.append(Job(
                source="rippling", company=company, job_id=jid,
                title=(j.get("name") or j.get("title") or "").strip(),
                location=" | ".join(l for l in locs if l),
                url=j.get("url") or f"https://ats.rippling.com/{slug}/jobs/{jid}",
                remote=any("remote" in l.lower() for l in locs) or None,
                posted_at=parse_iso(j.get("createdOn") or j.get("publishedAt") or j.get("created_at")),
                extra={"slug": slug},
            ))
        total_pages = data.get("totalPages") if isinstance(data, dict) else None
        page += 1
        if not items or not total_pages or page >= total_pages:
            break
    return jobs


def _enrich_rippling(job: Job) -> None:
    d = http.get_json(f"https://ats.rippling.com/api/v2/board/{job.extra['slug']}/jobs/{job.job_id}")
    desc = d.get("description")
    if isinstance(desc, dict):
        desc = " ".join(str(v) for v in desc.values())
    job.description = html_to_text(desc)
    locs = d.get("workLocations") or []
    if locs and not job.location:
        job.location = " | ".join(str(l) for l in locs)


# ---------- Workable ----------

def fetch_workable(company: str, slug: str, **_) -> list[Job]:
    data = http.get_json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}", params={"details": "true"})
    jobs = []
    for j in data.get("jobs", []):
        code = j.get("shortcode") or j.get("id")
        if not code:
            continue
        loc = ", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x)
        jobs.append(Job(
            source="workable", company=company, job_id=str(code),
            title=(j.get("title") or "").strip(), location=loc,
            url=j.get("url") or j.get("shortlink") or f"https://apply.workable.com/{slug}/j/{code}/",
            remote=bool(j.get("telecommuting")) or None,
            posted_at=parse_iso(j.get("published_on") or j.get("created_at")),
            description=html_to_text(j.get("description")),
            country_codes=[j["country"]] if j.get("country") else [],
        ))
    return jobs


# ---------- Recruitee ----------

def fetch_recruitee(company: str, slug: str, **_) -> list[Job]:
    data = http.get_json(f"https://{slug}.recruitee.com/api/offers/")
    jobs = []
    for j in data.get("offers", []):
        posted = (j.get("published_at") or j.get("created_at") or "").replace(" UTC", "+00:00").replace(" ", "T", 1)
        jobs.append(Job(
            source="recruitee", company=company, job_id=str(j.get("id")),
            title=(j.get("title") or "").strip(),
            location=j.get("location") or ", ".join(x for x in (j.get("city"), j.get("country")) if x),
            url=j.get("careers_url") or "",
            remote=bool(j.get("remote")) or None,
            posted_at=parse_iso(posted),
            description=html_to_text((j.get("description") or "") + " " + (j.get("requirements") or "")),
            country_codes=[j["country_code"].upper()] if j.get("country_code") else [],
        ))
    return jobs


# ---------- Atlassian (own listings feed) ----------

def fetch_atlassian(company: str = "Atlassian", **_) -> list[Job]:
    data = http.get_json("https://www.atlassian.com/endpoint/careers/listings", timeout=40)
    jobs = []
    for j in data if isinstance(data, list) else []:
        jid = j.get("id") or (j.get("portalJobPost") or {}).get("id")
        if not jid:
            continue
        locs = [str(l) for l in (j.get("locations") or [])]
        jobs.append(Job(
            source="atlassian", company=company, job_id=str(jid),
            title=(j.get("title") or "").strip(),
            location=" | ".join(locs),
            url=(j.get("portalJobPost") or {}).get("portalUrl")
                or f"https://www.atlassian.com/company/careers/details/{jid}",
            remote=any("remote" in l.lower() for l in locs) or None,
            description=html_to_text(" ".join(str(j.get(k) or "") for k in
                                              ("overview", "responsibilities", "qualifications")))[:6000],
        ))
    return jobs


# ---------- Oracle Recruiting Cloud (Uber and others) ----------

def fetch_oracle(company: str, host: str, site: str, search: str = "engineer", pages: int = 2, **_) -> list[Job]:
    jobs = []
    for page in range(pages):
        finder = (f"findReqs;siteNumber={site},keyword={quote(search)},sortBy=POSTING_DATES_DESC,"
                  f"limit=100,offset={page * 100}")
        data = http.get_json(f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
                             f"?onlyData=true&expand=requisitionList.secondaryLocations&finder={finder}", timeout=40)
        reqs = ((data.get("items") or [{}])[0]).get("requisitionList") or []
        for j in reqs:
            jid = str(j.get("Id") or "")
            if not jid:
                continue
            secondary = [x.get("Name", "") for x in (j.get("secondaryLocations") or [])]
            countries = [j.get("PrimaryLocationCountry")] + [x.get("CountryCode") for x in (j.get("secondaryLocations") or [])]
            jobs.append(Job(
                source="oracle", company=company, job_id=jid,
                title=(j.get("Title") or "").strip(),
                location=" | ".join(l for l in [j.get("PrimaryLocation") or ""] + secondary if l),
                url=f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{jid}",
                remote=("remote" in str(j.get("WorkplaceType") or j.get("WorkplaceTypeCode") or "").lower()) or None,
                posted_at=parse_iso(j.get("PostedDate")),
                description=html_to_text(" ".join(str(j.get(k) or "") for k in
                                                  ("ShortDescriptionStr", "ExternalQualificationsStr", "ExternalResponsibilitiesStr"))),
                country_codes=[c for c in countries if c],
            ))
        if len(reqs) < 100:
            break
    return jobs


# ---------- registry ----------

FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "workday": fetch_workday,
    "amazon": fetch_amazon,
    "microsoft": fetch_microsoft,
    "google": fetch_google,
    "rippling": fetch_rippling,
    "workable": fetch_workable,
    "recruitee": fetch_recruitee,
    "atlassian": fetch_atlassian,
    "oracle": fetch_oracle,
}

ENRICHERS = {
    "greenhouse": _enrich_greenhouse,
    "smartrecruiters": _enrich_smartrecruiters,
    "workday": _enrich_workday,
    "rippling": _enrich_rippling,
}

DISCOVERABLE = ["greenhouse", "lever", "ashby", "smartrecruiters", "rippling", "workable", "recruitee"]


class DiscoveryError(Exception):
    """Probing hit errors (rate limits, timeouts) and found nothing — retry later instead of caching 'not found'."""


def enrich(job: Job) -> None:
    fn = ENRICHERS.get(job.source)
    if fn and not job.description:
        try:
            fn(job)
        except Exception as e:  # enrichment is best-effort
            log.warning("enrich failed for %s: %s", job.uid, e)


def slug_candidates(name: str) -> list[str]:
    base = name.lower().replace("&", "and")
    words = re.findall(r"[a-z0-9]+", base)
    cands = ["".join(words), "-".join(words)]
    if words and words[-1] in ("ai", "labs", "inc", "technologies", "tech", "hq", "io", "app"):
        cands.append("".join(words[:-1]))
    out = []
    for c in cands:
        if c and c not in out:
            out.append(c)
    return out


def discover(name: str, slugs: list[str] | None = None) -> dict | None:
    """Probe the public ATS boards for a company name. Returns {'ats','slug','count'} for the best hit."""
    best, errors = None, []
    for ats in DISCOVERABLE:
        for slug in slugs or slug_candidates(name):
            try:
                jobs = FETCHERS[ats](company=name, slug=slug)
            except http.NotFound:
                continue
            except Exception as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                if ats == "recruitee" and status is None:  # unknown subdomain
                    continue
                if status in (400, 401, 403, 410, 422):   # board doesn't exist / not public
                    continue
                errors.append(f"{ats}/{slug}: {type(e).__name__} {status or ''}".strip())
                continue
            if jobs and (best is None or len(jobs) > best["count"]):
                best = {"ats": ats, "slug": slug, "count": len(jobs)}
            if jobs:
                break  # first working slug for this ATS is enough
    if best is None and errors:
        raise DiscoveryError("; ".join(errors[:3]))
    return best


def linkedin_people_search(company: str, role_hint: str = "engineering manager") -> str:
    return "https://www.linkedin.com/search/results/people/?keywords=" + quote(f"{company} {role_hint}")
