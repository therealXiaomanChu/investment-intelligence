"""Bounded HTTP with per-host pacing. Errors omit query strings, headers and response bodies."""

from email.utils import parsedate_to_datetime
from time import monotonic, sleep, time
from urllib.parse import urlsplit

import requests


MAX_RESPONSE_BYTES = 48 * 1024 * 1024


class SourceError(RuntimeError):
    pass


class Http:
    def __init__(self, env, *, session=None, pause=sleep):
        self.env = env
        self.session = session or requests.Session()
        self.pause = pause
        self.last = {}

    def get(self, url, headers=None, params=None):
        return self.request("GET", url, headers=headers, params=params)

    def post_json(self, url, payload, headers=None):
        return self.request("POST", url, headers=headers, json=payload)

    def request(self, method, url, **kwargs):
        parts = urlsplit(url)
        host = parts.hostname
        if parts.scheme != "https" or not host:
            raise SourceError("Public sources require HTTPS")
        sec = host == "sec.gov" or host.endswith(".sec.gov")
        agent = (
            self.env.get("SEC_USER_AGENT") if sec else "InvestmentIntelligenceOS/1.0 (public metadata reader)"
        )
        if sec and (not agent or "@" not in agent or "example." in agent.lower()):
            raise SourceError("Set SEC_USER_AGENT to an identifying organization and contact email")
        headers = {"User-Agent": agent, **(kwargs.pop("headers", None) or {})}
        group = "sec.gov" if sec else host
        interval = 3.1 if host.endswith("arxiv.org") else 0.35
        for attempt in range(3):
            self.pause(max(0, self.last.get(group, 0) + interval - monotonic()))
            self.last[group] = monotonic()
            retry = 2**attempt
            try:
                with self.session.request(
                    method, url, headers=headers, timeout=(6, 20), stream=True, **kwargs
                ) as response:
                    code = response.status_code
                    if code == 429 or 500 <= code <= 599:
                        header = response.headers.get("Retry-After", "")
                        if header:
                            try:
                                retry = max(
                                    retry,
                                    (
                                        float(header)
                                        if header.isdigit()
                                        else parsedate_to_datetime(header).timestamp() - time()
                                    ),
                                )
                            except (ValueError, TypeError, OverflowError):
                                pass
                        if retry > 30:
                            raise SourceError(f"{host} HTTP {code}, retry on next run")
                        failure = f"{host} HTTP {code}"
                    elif not 200 <= code < 300:
                        raise SourceError(f"{host} HTTP {code}")
                    else:
                        data = bytearray()
                        for chunk in response.iter_content(65536):
                            data.extend(chunk)
                            if len(data) > MAX_RESPONSE_BYTES:
                                raise SourceError(f"{host} response exceeds 48 MiB")
                        return bytes(data)
            except requests.RequestException as exc:
                failure = f"{host} {type(exc).__name__}"
            if attempt < 2:
                self.pause(retry)
        raise SourceError(failure)


class FixtureHttp:
    def __init__(self, phase=0):
        self.phase = phase

    def request(self, method, url, payload=None, params=None):
        from .collectors import corporate, macro, news, research

        if params:
            url = requests.Request(method, url, params=params).prepare().url
        for module in (corporate, macro, news, research):
            result = module.demo_response(url, method, payload, self.phase)
            if result is not None:
                return result
        parts = urlsplit(url)
        raise SourceError(f"No offline response registered for {parts.hostname}{parts.path}")

    def get(self, url, headers=None, params=None):
        return self.request("GET", url, params=params)

    def post_json(self, url, payload, headers=None):
        return self.request("POST", url, payload)
