"""Tiny JSON state, committed back to the repo by the workflow (only when it changes)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC = timezone.utc
DISCOVERY_VERSION = 2


class State:
    def __init__(self, path: Path):
        self.path = path
        self.data = {"seen": {}, "seeded": [], "discovery": {}}
        if path.exists():
            self.data.update(json.loads(path.read_text() or "{}"))
        self._orig = json.dumps(self.data, sort_keys=True)
        if self.data.get("discovery_version") != DISCOVERY_VERSION:
            # earlier discovery runs could record "not found" after being rate-limited: re-check those
            self.data["discovery"] = {k: v for k, v in self.data["discovery"].items() if v.get("ats")}
            self.data["discovery_version"] = DISCOVERY_VERSION

    # seen jobs -------------------------------------------------------
    def is_seen(self, uid: str) -> bool:
        return uid in self.data["seen"]

    def mark_seen(self, uid: str) -> None:
        self.data["seen"].setdefault(uid, datetime.now(UTC).strftime("%Y-%m-%dT%H:%MZ"))

    # a company's jobs are swallowed silently the first time we read it
    def is_seeded(self, key: str) -> bool:
        return key in self.data["seeded"]

    def mark_seeded(self, key: str) -> None:
        if key not in self.data["seeded"]:
            self.data["seeded"].append(key)

    # auto-discovery cache --------------------------------------------
    def discovery(self, name: str) -> dict | None:
        return self.data["discovery"].get(name)

    def set_discovery(self, name: str, result: dict | None) -> None:
        self.data["discovery"][name] = {
            **(result or {"ats": None}),
            "checked": datetime.now(UTC).strftime("%Y-%m-%d"),
        }

    def discovery_stale(self, name: str, days: int = 7) -> bool:
        d = self.discovery(name)
        if not d:
            return True
        if d.get("ats"):
            return False
        try:
            return datetime.strptime(d["checked"], "%Y-%m-%d").replace(tzinfo=UTC) < datetime.now(UTC) - timedelta(days=days)
        except (KeyError, ValueError):
            return True

    # persistence -----------------------------------------------------
    def prune(self, days: int = 120) -> None:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%MZ")
        self.data["seen"] = {k: v for k, v in self.data["seen"].items() if v >= cutoff}

    def save(self) -> bool:
        new = json.dumps(self.data, sort_keys=True)
        if new == self._orig:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=0, sort_keys=True) + "\n")
        self._orig = new
        return True
