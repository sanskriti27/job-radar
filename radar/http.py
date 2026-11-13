from __future__ import annotations

import logging
import time

import requests
from requests.adapters import HTTPAdapter

log = logging.getLogger("radar.http")

_session = requests.Session()
_adapter = HTTPAdapter(pool_connections=32, pool_maxsize=32)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)
_session.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
})

TIMEOUT = 20
RETRY_STATUSES = {429, 500, 502, 503, 504}


class NotFound(Exception):
    pass


def _request(method: str, url: str, **kw) -> requests.Response:
    kw.setdefault("timeout", TIMEOUT)
    for attempt in range(3):
        try:
            r = _session.request(method, url, **kw)
        except (requests.ConnectionError, requests.Timeout):
            if attempt == 2:
                raise
            time.sleep(1.5 * (attempt + 1))
            continue
        if r.status_code == 404:
            raise NotFound(url)
        if r.status_code in RETRY_STATUSES and attempt < 2:
            wait = float(r.headers.get("Retry-After") or 0) or 2.0 * (attempt + 1)
            time.sleep(min(wait, 10))
            continue
        r.raise_for_status()
        return r
    r.raise_for_status()
    return r


def get(url: str, params: dict | None = None, timeout: int = TIMEOUT) -> requests.Response:
    return _request("GET", url, params=params, timeout=timeout)


def get_json(url: str, params: dict | None = None, timeout: int = TIMEOUT):
    return get(url, params=params, timeout=timeout).json()


def post_json(url: str, payload: dict, headers: dict | None = None, timeout: int = TIMEOUT):
    r = _request("POST", url, json=payload, headers=headers or {}, timeout=timeout)
    return r.json() if r.content else None
