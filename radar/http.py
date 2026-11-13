from __future__ import annotations

import logging

import requests

log = logging.getLogger("radar.http")

_session = requests.Session()
_session.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
})

TIMEOUT = 20


class NotFound(Exception):
    pass


def get(url: str, params: dict | None = None, timeout: int = TIMEOUT) -> requests.Response:
    r = _session.get(url, params=params, timeout=timeout)
    if r.status_code == 404:
        raise NotFound(url)
    r.raise_for_status()
    return r


def get_json(url: str, params: dict | None = None, timeout: int = TIMEOUT):
    return get(url, params=params, timeout=timeout).json()


def post_json(url: str, payload: dict, headers: dict | None = None, timeout: int = TIMEOUT):
    r = _session.post(url, json=payload, headers=headers or {}, timeout=timeout)
    if r.status_code == 404:
        raise NotFound(url)
    r.raise_for_status()
    return r.json() if r.content else None
