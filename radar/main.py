from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml

from . import notify, scorer, sources
from .filters import TitleFilter, description_mentions_india, location_verdict
from .models import Job
from .state import State

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
STATE_PATH = ROOT / "state" / "seen.json"
FLOOD_LIMIT = 25          # more new jobs than this from one board in one run = treat as a re-seed, not news
DISCOVERIES_PER_RUN = 40  # keep each run short; the rest are discovered on later runs

log = logging.getLogger("radar")
SENIOR_RE = re.compile(r"\b(senior|sr)\b", re.I)


# ---------- config ----------

def load_config() -> tuple[dict, dict, str]:
    watch = yaml.safe_load((CONFIG / "watchlist.yaml").read_text()) or {}
    filt = yaml.safe_load((CONFIG / "filters.yaml").read_text()) or {}
    profile = os.environ.get("CANDIDATE_PROFILE") or (CONFIG / "profile.md").read_text()
    return watch, filt, profile


def resolve_sources(watch: dict, state: State, force_discover: bool = False) -> tuple[list[dict], list[str]]:
    """Pinned entries + auto names resolved through (cached) discovery. Returns (sources, unresolved names)."""
    srcs = [dict(e) for e in watch.get("pinned") or []]
    pinned_names = {e["name"].lower() for e in srcs}
    names = [n for n in (watch.get("auto") or []) if n.lower() not in pinned_names]

    to_probe = [n for n in names if state.discovery_stale(n)]
    if not force_discover:
        to_probe = to_probe[:DISCOVERIES_PER_RUN]
    if to_probe:
        log.info("discovering %d companies…", len(to_probe))
        with ThreadPoolExecutor(8) as ex:   # gentle: too many parallel probes get rate-limited
            futs = {ex.submit(sources.discover, n): n for n in to_probe}
            for f in as_completed(futs):
                n = futs[f]
                try:
                    res = f.result()
                except Exception as e:      # rate-limited / timed out: try again next run, don't cache
                    log.warning("  %-22s → retry later (%s)", n, e)
                    continue
                state.set_discovery(n, res)
                log.info("  %-22s → %s", n, f"{res['ats']}/{res['slug']} ({res['count']} jobs)" if res else "not found")

    unresolved = []
    for n in names:
        d = state.discovery(n) or {}
        if d.get("ats"):
            srcs.append({"name": n, "ats": d["ats"], "slug": d["slug"]})
        else:
            unresolved.append(n)
    return srcs, unresolved


def fetch_all(srcs: list[dict]) -> tuple[dict[str, list[Job]], dict[str, str]]:
    results, errors = {}, {}

    def one(src):
        params = {k: v for k, v in src.items() if k not in ("ats", "name")}
        return sources.FETCHERS[src["ats"]](company=src["name"], **params)

    # Workday rate-limits hard (429s), so those are read one at a time; everything else in parallel.
    slow = [s for s in srcs if s["ats"] == "workday"]
    fast = [s for s in srcs if s["ats"] != "workday"]
    with ThreadPoolExecutor(16) as ex, ThreadPoolExecutor(1) as ex_slow:
        futs = {ex.submit(one, s): s for s in fast}
        futs.update({ex_slow.submit(one, s): s for s in slow})
        for f in as_completed(futs):
            s = futs[f]
            key = f"{s['ats']}:{s['name']}"
            try:
                results[key] = f.result()
            except Exception as e:
                errors[key] = f"{type(e).__name__}: {e}"[:200]
    return results, errors


# Sources that block when polled too often are read at most this often (minutes) when the radar loops.
MIN_INTERVAL_MIN = {"workday": 12, "google": 8, "microsoft": 8, "atlassian": 6, "oracle": 6}
THROTTLE_PATH = ROOT / "state" / "throttle.json"   # not committed; lives for the length of one loop job


def due_sources(srcs: list[dict]) -> list[dict]:
    import json
    now = time.time()
    try:
        last = json.loads(THROTTLE_PATH.read_text())
    except (OSError, ValueError):
        last = {}
    due = []
    for s in srcs:
        key = f"{s['ats']}:{s['name']}"
        gap = MIN_INTERVAL_MIN.get(s["ats"], 0) * 60
        if gap and now - last.get(key, 0) < gap:
            continue
        if gap:
            last[key] = now
        due.append(s)
    try:
        THROTTLE_PATH.parent.mkdir(parents=True, exist_ok=True)
        THROTTLE_PATH.write_text(json.dumps(last))
    except OSError:
        pass
    return due


# ---------- filtering ----------

def shortlist(jobs: list[Job], title_ok: TitleFilter, max_age_h: float) -> list[Job]:
    keep, need_detail = [], []
    for j in jobs:
        if not title_ok.ok(j.title):
            continue
        age = j.age_hours()
        if age is not None and age > max_age_h:
            continue
        v = location_verdict(j)
        if v == "yes":
            keep.append(j)
        elif v in ("maybe", "unknown"):
            need_detail.append(j)

    _enrich_many(keep + need_detail)
    for j in need_detail:
        v = location_verdict(j)
        if v == "yes" or (v in ("maybe", "unknown") and description_mentions_india(j)):
            keep.append(j)
    return keep


def _enrich_many(jobs: list[Job]) -> None:
    if not jobs:
        return
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(sources.enrich, jobs))


# ---------- commands ----------

def cmd_run() -> int:
    t0 = time.time()
    watch, filt, profile = load_config()
    state = State(STATE_PATH)
    srcs, unresolved = resolve_sources(watch, state)
    polled = due_sources(srcs)
    results, errors = fetch_all(polled)

    fresh: list[Job] = []
    seeded_now = []
    for key, jobs in results.items():
        new = [j for j in jobs if not state.is_seen(j.uid)]
        if not state.is_seeded(key) or len(new) > FLOOD_LIMIT:
            for j in jobs:
                state.mark_seen(j.uid)
            state.mark_seeded(key)
            seeded_now.append(f"{key} ({len(jobs)})")
            continue
        for j in new:
            state.mark_seen(j.uid)
        fresh.extend(new)

    title_ok = TitleFilter(filt.get("title_include", []), filt.get("title_exclude", []))
    cands = shortlist(fresh, title_ok, float(filt.get("max_age_hours", 72)))
    scores = scorer.score(cands, profile)

    min_score = int(filt.get("min_score", 6))
    picks = []
    for j in cands:
        s = scores.get(j.uid)
        if s is None and SENIOR_RE.search(j.title):
            continue   # Gemini unavailable: can't check the years asked, so don't ping for senior titles
        if s is None or (s.get("location_ok", True) and int(s.get("score", 0)) >= min_score):
            picks.append((j, s))
    picks.sort(key=lambda p: -(int(p[1]["score"]) if p[1] and p[1].get("score") is not None else 5))

    cap = int(filt.get("max_individual_alerts", 12))
    for j, s in picks[:cap]:
        notify.alert(j, s)
    if len(picks) > cap:
        notify.summary(f"+{len(picks) - cap} more matches", [f"{j.company}: {j.title} — {j.url}" for j, _ in picks[cap:]])

    state.prune()
    changed = state.save()

    report = [
        f"### Job Radar run — {len(polled)}/{len(srcs)} boards, {sum(len(v) for v in results.values())} open roles read, {time.time() - t0:.1f}s",
        f"- new since last run: **{len(fresh)}** · passed filters: **{len(cands)}** · alerted: **{len(picks)}**",
    ]
    if seeded_now:
        report.append(f"- first read (recorded silently): {', '.join(seeded_now)}")
    if errors:
        report.append(f"- ⚠️ {len(errors)} boards failed: " + "; ".join(f"`{k}` {v}" for k, v in sorted(errors.items())))
    if unresolved:
        report.append(f"- not found on any public board (yet): {', '.join(unresolved)}")
    for j, s in picks:
        report.append(f"- 🎯 {j.company} — [{j.title}]({j.url}) · {j.location} · score {s.get('score') if s else '?'}")
    if not notify.configured():
        report.append("- ⚠️ no NTFY_TOPIC / TELEGRAM_* secrets set — alerts were not sent")
    _emit("\n".join(report))
    log.info("state %s", "updated" if changed else "unchanged")
    return 0


def cmd_validate() -> int:
    watch, filt, _ = load_config()
    state = State(STATE_PATH)
    srcs, unresolved = resolve_sources(watch, state, force_discover=True)
    results, errors = fetch_all(srcs)
    title_ok = TitleFilter(filt.get("title_include", []), filt.get("title_exclude", []))
    rows = [f"**Gemini scoring:** {scorer.selftest()}",
            f"**Phone alerts:** {'✅ configured' if notify.configured() else '❌ NTFY_TOPIC secret is not set'}", "",
            "| Company | Board | Open roles | Relevant now (title + India) |", "|---|---|---|---|"]
    for s in sorted(srcs, key=lambda x: x["name"].lower()):
        key = f"{s['ats']}:{s['name']}"
        board = s["ats"] + (f"/{s.get('slug')}" if s.get("slug") else "")
        if key in errors:
            rows.append(f"| {s['name']} | {board} | ❌ {errors[key][:80]} | |")
            continue
        jobs = results.get(key, [])
        rel = sum(1 for j in jobs if title_ok.ok(j.title) and location_verdict(j) in ("yes", "unknown", "maybe"))
        rows.append(f"| {s['name']} | {board} | {len(jobs)} | {rel} |")
    not_found = [n for n in unresolved if state.discovery(n)]
    retry = [n for n in unresolved if not state.discovery(n)]
    if not_found:
        rows.append("\n**Not on Greenhouse/Lever/Ashby/SmartRecruiters/Rippling/Workable/Recruitee** (pin them in watchlist.yaml if you know the board): "
                    + ", ".join(not_found))
    if retry:
        rows.append("\n**Couldn't check yet (rate-limited/timeouts) — will retry automatically:** " + ", ".join(retry))
    _emit("\n".join(rows))
    return 1 if errors and len(errors) == len(srcs) else 0


def cmd_test_notify() -> int:
    if not notify.configured():
        print("Set NTFY_TOPIC (or TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID) first.")
        return 1
    from datetime import datetime, timedelta, timezone
    job = Job(source="test", company="Example Labs", job_id="1", title="Software Engineer II (Backend)",
              location="Hyderabad, India", url="https://example.com/jobs/1",
              posted_at=datetime.now(timezone.utc) - timedelta(minutes=7))
    notify.alert(job, {"score": 9, "years": "2-4 yrs", "why": "Java/Kafka microservices, fintech — test alert"})
    print("Test alert sent.")
    return 0


def cmd_discover(names: list[str]) -> int:
    for n in names:
        res = sources.discover(n)
        print(f"{n:24} → {res['ats'] + '/' + res['slug'] + ' (' + str(res['count']) + ' jobs)' if res else 'not found'}")
    return 0


def cmd_probe(urls: list[str]) -> int:
    """Debug helper: fetch URLs from the runner and show what comes back (status, start of body, ATS hints)."""
    from . import http
    hint = re.compile(r"""https?:[^"'\s<>]*(?:greenhouse|lever\.co|ashby|workday|darwinbox|keka|oraclecloud|"""
                      r"""smartrecruiters|freshteam|zohorecruit|/api/|endpoint|graphql)[^"'\s<>]*""", re.I)
    src = re.compile(r"""<script[^>]+src=["']([^"']+)""", re.I)
    for u in urls:
        try:
            r = http._session.get(u, timeout=25)
            body = r.text
            print(f"\n=== {u}\n{r.status_code} {r.headers.get('content-type')} len={len(body)} final={r.url}")
            print("HEAD:", body[:500].replace("\n", " "))
            for h in list(dict.fromkeys(hint.findall(body.replace("\\/", "/"))))[:30]:
                print("  hint:", h[:200])
            if "json" in (r.headers.get("content-type") or ""):
                import json as _json
                def cut(o, d=0):
                    if isinstance(o, dict):
                        return {k: cut(v, d + 1) for k, v in list(o.items())[:40]}
                    if isinstance(o, list):
                        return [cut(v, d + 1) for v in o[:1]] + ([f"…{len(o)} items"] if len(o) > 1 else [])
                    return o[:70] if isinstance(o, str) else o
                try:
                    print("JSON:", _json.dumps(cut(r.json()))[:3500])
                except ValueError:
                    pass
                continue
            kw = re.compile(r""".{0,90}(?:greenhouse|lever\.co|ashbyhq|darwinbox|keka\.com|myworkday|eightfold|icims|"""
                            r"""kula\.ai|zohorecruit|freshteam|turbohire|peoplestrong|oraclecloud|smartrecruiters|"""
                            r"""job-openings|jobOpenings|/jobs|openings\b|positions\b).{0,140}""", re.I)
            from urllib.parse import urljoin
            texts = [("page", body)]
            for h in list(dict.fromkeys(src.findall(body)))[:40]:
                try:
                    texts.append((h[-50:], http._session.get(urljoin(r.url, h), timeout=25).text))
                except Exception:
                    pass
            for name, t in texts:
                hits = [m for m in dict.fromkeys(kw.findall(t)) if re.search(r"https?:|fetch|axios|api|\.json", m, re.I)]
                for m in hits[:8]:
                    print(f"  [{name}] {m.strip()[:230]}")
        except Exception as e:
            print(f"\n=== {u}\nERROR {type(e).__name__}: {e}"[:400])
    return 0


def _emit(md: str) -> None:
    print(md)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(md + "\n")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="radar")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("run", help="poll boards and alert on new matches (default)")
    sub.add_parser("validate", help="check every board in the watchlist and print a table")
    sub.add_parser("test-notify", help="send a sample alert to your phone")
    d = sub.add_parser("discover", help="find which public board a company uses")
    d.add_argument("names", nargs="+")
    pr = sub.add_parser("probe", help="debug: show what a URL returns from here")
    pr.add_argument("urls", nargs="+")
    a = p.parse_args(argv)
    if a.cmd in (None, "run"):
        return cmd_run()
    if a.cmd == "validate":
        return cmd_validate()
    if a.cmd == "test-notify":
        return cmd_test_notify()
    if a.cmd == "discover":
        return cmd_discover(a.names)
    if a.cmd == "probe":
        return cmd_probe(a.urls)
    return 2


if __name__ == "__main__":
    sys.exit(main())
