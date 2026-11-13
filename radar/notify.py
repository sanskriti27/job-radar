"""Push alerts: ntfy (default, no signup) and/or Telegram, whichever secrets are set."""
from __future__ import annotations

import logging
import os

from . import http
from .models import Job
from .sources import linkedin_people_search

log = logging.getLogger("radar.notify")


def _age(job: Job) -> str:
    h = job.age_hours()
    if h is None:
        return "just spotted"
    if h < 1:
        return f"{max(1, int(h * 60))} min ago"
    if h < 48:
        return f"{int(h)} h ago"
    return f"{int(h // 24)} d ago"


def format_job(job: Job, s: dict | None) -> tuple[str, str]:
    sc = s.get("score") if s else None
    title = f"{'🔥' if (sc or 0) >= 8 else '🎯'} {job.company}: {job.title}"
    lines = [
        f"📍 {job.location or 'location n/a'}{' · remote' if job.remote else ''}",
        f"🕒 posted {_age(job)} · match {sc if sc is not None else '?'}/10"
        + (f" · {s['years']}" if s and s.get("years") else ""),
    ]
    if s and s.get("why"):
        lines.append(f"💡 {s['why']}")
    return title, "\n".join(lines)


def send_ntfy(title: str, message: str, url: str = "", actions: list | None = None, priority: int = 4) -> None:
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    body = {"topic": topic, "title": title[:250], "message": message, "priority": priority, "tags": ["briefcase"]}
    if url:
        body["click"] = url
    if actions:
        body["actions"] = actions
    headers = {}
    if os.environ.get("NTFY_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['NTFY_TOKEN']}"
    http.post_json(server, body, headers=headers)


def send_telegram(title: str, message: str, url: str = "") -> None:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    text = f"<b>{_esc(title)}</b>\n{_esc(message)}" + (f"\n<a href=\"{url}\">Apply →</a>" if url else "")
    http.post_json(f"https://api.telegram.org/bot{token}/sendMessage",
                   {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def configured() -> bool:
    return bool(os.environ.get("NTFY_TOPIC") or (os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")))


def alert(job: Job, s: dict | None) -> None:
    title, msg = format_job(job, s)
    sc = (s or {}).get("score") or 0
    actions = [
        {"action": "view", "label": "Apply", "url": job.url, "clear": False},
        {"action": "view", "label": "Find hiring mgr", "url": linkedin_people_search(job.company), "clear": False},
    ]
    for fn, kw in ((send_ntfy, {"url": job.url, "actions": actions, "priority": 5 if sc >= 8 else 4}),
                   (send_telegram, {"url": job.url})):
        try:
            fn(title, msg, **kw)
        except Exception as e:
            log.error("%s failed: %s", fn.__name__, e)


def summary(title: str, lines: list[str]) -> None:
    msg = "\n".join(lines)[:3500]
    for fn in (send_ntfy, send_telegram):
        try:
            fn(title, msg) if fn is send_telegram else fn(title, msg, priority=3)
        except Exception as e:
            log.error("%s failed: %s", fn.__name__, e)
