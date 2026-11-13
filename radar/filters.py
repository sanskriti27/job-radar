"""Rule-based first pass: title and location (India, or remote open to India)."""
from __future__ import annotations

import re

from .models import Job

INDIA_PLACES = [
    "india", "bengaluru", "bangalore", "hyderabad", "secunderabad", "pune", "mumbai", "navi mumbai",
    "gurgaon", "gurugram", "noida", "greater noida", "new delhi", "delhi", "ncr", "chennai", "kolkata",
    "ahmedabad", "jaipur", "kochi", "cochin", "trivandrum", "thiruvananthapuram", "indore", "coimbatore",
    "chandigarh", "mohali", "nagpur", "vadodara", "surat", "lucknow", "bhubaneswar", "mysore", "mysuru",
    "visakhapatnam", "vizag", "goa", "karnataka", "telangana", "maharashtra", "tamil nadu", "haryana",
]
INDIA_RE = re.compile(r"\b(" + "|".join(re.escape(p) for p in INDIA_PLACES) + r")\b", re.I)
INDIA_CODE_RE = re.compile(r"(,|\s|^)IND$")               # "Hyderabad, TS, IND" (not ", IN" = Indiana)
REMOTE_RE = re.compile(r"\b(remote|anywhere|work from home|wfh|distributed)\b", re.I)
REMOTE_OPEN_RE = re.compile(r"\b(apac|asia|asia[- ]pacific|worldwide|anywhere|global|globally|emea\s*&\s*apac)\b", re.I)
COUNTRY_INDIA = {"IN", "IND", "INDIA"}


class TitleFilter:
    def __init__(self, include: list[str], exclude: list[str]):
        self.inc = [re.compile(p, re.I) for p in include]
        self.exc = [re.compile(p, re.I) for p in exclude]

    def ok(self, title: str) -> bool:
        t = title or ""
        return any(p.search(t) for p in self.inc) and not any(p.search(t) for p in self.exc)


def location_verdict(job: Job) -> str:
    """'yes' = in India or remote-open-to-India; 'maybe' = remote/unknown, let Gemini decide; 'no' otherwise."""
    loc = job.location or ""
    if any(c.strip().upper() in COUNTRY_INDIA for c in job.country_codes):
        return "yes"
    if INDIA_RE.search(loc) or any(INDIA_CODE_RE.search(part.strip()) for part in loc.split("|")):
        return "yes"
    is_remote = bool(job.remote) or bool(REMOTE_RE.search(loc))
    if is_remote:
        if REMOTE_OPEN_RE.search(loc):
            return "yes"
        # "Remote" with no region, or remote in another named country → let the description decide
        stripped = REMOTE_RE.sub("", loc).strip(" -,|()/")
        return "maybe" if not stripped else "maybe_foreign"
    if not loc.strip() or re.fullmatch(r"\d+\s+locations?", loc.strip(), re.I) or loc.strip().lower() in ("multiple locations", "various"):
        return "unknown"
    return "no"


def description_mentions_india(job: Job) -> bool:
    return bool(INDIA_RE.search(job.description or "")) or bool(REMOTE_OPEN_RE.search(job.description or ""))
