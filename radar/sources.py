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
    data = http.get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
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


def _enrich_greenhouse(job: Job) -> None:
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
                  pages: int = 5, **_) -> list[Job]:
    base = f"https://{host}/wday/cxs/{tenant}/{site}"
    jobs = []
    for page in range(pages):
        data = http.post_json(f"{base}/jobs", {"appliedFacets": {}, "limit": 20, "offset": page * 20,
                                               "searchText": search})
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
}

ENRICHERS = {
    "greenhouse": _enrich_greenhouse,
    "smartrecruiters": _enrich_smartrecruiters,
    "workday": _enrich_workday,
}

DISCOVERABLE = ["greenhouse", "lever", "ashby", "smartrecruiters"]


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
    best = None
    for ats in DISCOVERABLE:
        for slug in slugs or slug_candidates(name):
            try:
                jobs = FETCHERS[ats](company=name, slug=slug)
            except http.NotFound:
                continue
            except Exception as e:
                log.debug("discover %s/%s: %s", ats, slug, e)
                continue
            if jobs and (best is None or len(jobs) > best["count"]):
                best = {"ats": ats, "slug": slug, "count": len(jobs)}
            if jobs:
                break  # first working slug for this ATS is enough
    return best


def linkedin_people_search(company: str, role_hint: str = "engineering manager") -> str:
    return "https://www.linkedin.com/search/results/people/?keywords=" + quote(f"{company} {role_hint}")
