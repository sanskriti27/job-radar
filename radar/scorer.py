"""Gemini scoring: one batched call per run for the new jobs that passed the rule filter."""
from __future__ import annotations

import json
import logging
import os

from . import http
from .models import Job

log = logging.getLogger("radar.scorer")

PROMPT = """You screen job postings for one candidate. Be strict and fast.

CANDIDATE
{profile}

For EACH job below return an object:
- "uid": copy exactly
- "score": integer 1-10 (fit for THIS candidate: stack overlap, realistic experience range for ~2 years, role type, company quality). 8+ = apply right now. 5 or below = skip.
- "location_ok": true only if the job is in India, or is remote and open to candidates living in India. If the posting is remote but restricted to another country/region, false. If genuinely unclear, true.
- "years": the experience the posting asks for, e.g. "2-4 yrs", or "" if not stated
- "why": max 12 words, why it fits or the main red flag

Return ONLY a JSON array.

JOBS
{jobs}
"""


def score(jobs: list[Job], profile: str) -> dict[str, dict]:
    """Returns {uid: {score, location_ok, years, why}}. Empty dict if Gemini isn't configured or fails."""
    key = os.environ.get("GEMINI_API_KEY")
    if not key or not jobs:
        return {}
    model = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
    out: dict[str, dict] = {}
    for i in range(0, len(jobs), 15):
        batch = jobs[i:i + 15]
        payload_jobs = [{
            "uid": j.uid, "company": j.company, "title": j.title, "location": j.location,
            "remote": j.remote, "description": (j.description or "")[:2500],
        } for j in batch]
        body = {
            "contents": [{"role": "user", "parts": [{"text": PROMPT.format(
                profile=profile, jobs=json.dumps(payload_jobs, ensure_ascii=False))}]}],
            "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
        }
        try:
            resp = http.post_json(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                body, headers={"x-goog-api-key": key}, timeout=60)
            text = resp["candidates"][0]["content"]["parts"][0]["text"]
            for item in json.loads(text):
                if isinstance(item, dict) and item.get("uid"):
                    out[item["uid"]] = item
        except Exception as e:
            log.warning("Gemini scoring failed (%s); these jobs will alert unscored", e)
    return out
