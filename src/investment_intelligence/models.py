"""Validated interchange types. Dates are timezone-aware, amounts retain source precision."""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
from urllib.parse import urlsplit


def utc(value):
    date = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if date.tzinfo is None or date.utcoffset() is None:
        raise ValueError(f"Missing time zone in {value!r}")
    return date.astimezone(timezone.utc).isoformat(timespec="microseconds")


def web_url(value):
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("Evidence must have a public HTTP(S) URL")
    return value


def encode(value):
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str, allow_nan=False
    )


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


@dataclass(frozen=True)
class Fact:
    metric: str
    value: str | int | float | Decimal | None
    unit: str = ""
    period: str = ""
    vintage: str = ""

    def __post_init__(self):
        if not self.metric or not isinstance(self.value, (str, int, float, Decimal, type(None))):
            raise ValueError("Invalid fact")
        if isinstance(self.value, (float, Decimal)) and not math.isfinite(self.value):
            raise ValueError("Nonfinite fact")


@dataclass(frozen=True)
class Record:
    source_id: str
    external_id: str
    url: str
    module: str
    kind: str
    event_key: str
    title: str
    summary: str
    event_at: str
    published_at: str | None
    status: str = "published"
    assets: tuple = ()
    facts: tuple = ()
    importance: float = 0.5
    scheduled_at: str | None = None
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        web_url(self.url)
        if self.module not in {"corporate", "research", "macro", "news"}:
            raise ValueError(f"Unknown module {self.module}")
        if not all((self.external_id, self.title, self.summary, self.kind, self.event_key, self.source_id)):
            raise ValueError("Missing document or event identity/title/summary")
        if not 0 <= self.importance <= 1 or not all(isinstance(f, Fact) for f in self.facts):
            raise ValueError("Invalid importance or facts")
        for key in ("event_at", "published_at", "scheduled_at"):
            value = getattr(self, key)
            if value is not None:
                object.__setattr__(self, key, utc(value))

    def payload(self):
        return asdict(self)


@dataclass(frozen=True)
class Signal:
    one_liner: str
    importance: float
    direction: float | None
    confidence: float | None
    horizon: str
    evidence_urls: list

    def validate(self, allowed_urls):
        if not isinstance(self.one_liner, str) or not (1 <= len(self.one_liner.strip()) <= 2000):
            raise ValueError("Invalid signal text")
        for value, minimum in ((self.importance, 0), (self.confidence, 0), (self.direction, -1)):
            if value is not None and (type(value) not in (int, float) or not minimum <= value <= 1):
                raise ValueError("Signal score out of bounds")
        if self.importance is None or self.horizon not in {
            "immediate",
            "days",
            "weeks",
            "months",
            "long_term",
        }:
            raise ValueError("Invalid importance or horizon")
        if (
            not isinstance(self.evidence_urls, list)
            or not self.evidence_urls
            or not set(self.evidence_urls) <= set(allowed_urls)
        ):
            raise ValueError("Unsupported evidence URL")
