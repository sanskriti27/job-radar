from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Job:
    source: str            # greenhouse / lever / ashby / smartrecruiters / workday / amazon / microsoft / google
    company: str           # display name
    job_id: str            # id inside the source
    title: str
    location: str = ""     # human-readable, may list several places joined by " | "
    url: str = ""
    remote: bool | None = None
    posted_at: datetime | None = None
    description: str = ""  # plain text, may be empty until enriched
    country_codes: list[str] = field(default_factory=list)  # structured country info when the API gives it
    extra: dict = field(default_factory=dict)  # source-specific bits needed for enrichment

    @property
    def uid(self) -> str:
        return f"{self.source}:{self.company.lower()}:{self.job_id}"

    def age_hours(self, now: datetime | None = None) -> float | None:
        if not self.posted_at:
            return None
        now = now or datetime.now(timezone.utc)
        return (now - self.posted_at).total_seconds() / 3600
