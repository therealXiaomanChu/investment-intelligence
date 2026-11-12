"""Verify BLS release clocks against the dated publication itself."""

import calendar
from datetime import date, datetime, timedelta
from io import BytesIO
import re
from urllib.parse import urljoin, urlsplit
from xml.etree.ElementTree import ParseError
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from defusedxml.ElementTree import fromstring
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from ..http import SourceError

PROGRAMS = {
    "cpi": ("cpi", "Consumer Price Index"),
    "employment": ("empsit", "The Employment Situation"),
    "ppi": ("ppi", "Producer Price Indexes"),
}
ATOM = "{http://www.w3.org/2005/Atom}"
NY = ZoneInfo("America/New_York")
DOL_INDEX = "https://www.dol.gov/newsroom/economicdata"


def _archive_links(http, code):
    raw = http.get(f"https://www.bls.gov/feed/{code}.rss")
    if not raw:
        return []
    feed = fromstring(raw)
    if feed.tag != ATOM + "feed" or feed.findtext(ATOM + "id") != f"bls.gov:feed:{code}":
        return []
    entries = []
    for link in feed.findall(ATOM + "entry/" + ATOM + "link"):
        url = urlsplit(urljoin("https://www.bls.gov", link.get("href", "")))
        match = re.fullmatch(rf"/news\.release/archives/{code}_(\d{{8}})\.htm", url.path)
        if url.scheme == "https" and url.hostname == "www.bls.gov" and match:
            entries.append(datetime.strptime(match[1], "%m%d%Y").date())
    return sorted(set(entries))


def _dol_index(http):
    page = BeautifulSoup(http.get(DOL_INDEX) or b"", "html.parser")
    dates = {}
    for anchor in page.select("a[href]"):
        url = urlsplit(urljoin(DOL_INDEX + "/", anchor["href"]))
        match = re.fullmatch(r"/newsroom/economicdata/(cpi|empsit|ppi)_(\d{8})\.pdf", url.path)
        if url.scheme == "https" and url.hostname == "www.dol.gov" and match:
            dates.setdefault(match[1], set()).add(datetime.strptime(match[2], "%m%d%Y").date())
    return dates


def _calendar_dates(ctx, group, period):
    # A closed event can have a later-discovered calendar document that did not become
    # the event's current document. Its date is a locator only, never publication proof.
    archive = getattr(ctx, "archive", None)
    if archive is None:
        return []
    rows = archive.all(
        "SELECT event_at,canonical_url FROM documents WHERE source_id='bls_calendar' AND external_id=:key",
        key=f"bls:{group}:{period}",
    )
    return sorted({
        datetime.fromisoformat(row["event_at"]).astimezone(NY).date()
        for row in rows if urlsplit(row["canonical_url"]).hostname in {"www.bls.gov", "www.newyorkfed.org"}
    })


def _candidate_days(ctx, group, code, period, feeds):
    if group not in feeds:
        try:
            feeds[group] = _archive_links(ctx.http, code), None
        except (SourceError, ParseError, ValueError) as exc:
            feeds[group] = [], str(exc) if isinstance(exc, SourceError) else type(exc).__name__
    yield "BLS", *feeds[group]
    if DOL_INDEX not in feeds:
        try:
            feeds[DOL_INDEX] = _dol_index(ctx.http), None
        except (SourceError, ValueError) as exc:
            feeds[DOL_INDEX] = {}, str(exc) if isinstance(exc, SourceError) else type(exc).__name__
    dol, error = feeds[DOL_INDEX]
    yield "DOL", sorted(dol.get(code, ())), error
    yield "calendar", _calendar_dates(ctx, group, period), None


def _release_clock(text, heading, archive_day, now):
    # The Atom entry itself may be prepared before the embargo expires.
    text = " ".join(text.split())
    title = re.search(
        r"\b(Consumer Price Index|Producer Price Indexes|The Employment Situation)\s*[-–—]+\s*"
        r"([A-Za-z]+ \d{4})\b", text[:3500], re.I,
    )
    clock = re.search(
        r"transmission of material in this (?:news )?release is embargoed until\s+"
        r"(?:USDL[-\s]+\d{2}-\d+\s+)?(\d{1,2}:\d{2})\s*([ap])\.?m\.?\s*"
        r"\(ET\)\s+(\w+),\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})", text[:3500], re.I,
    )
    if not title or title[1].casefold() != heading.casefold() or not clock:
        return None
    period = datetime.strptime(title[2], "%B %Y").strftime("%Y-%m")
    stamp = datetime.strptime(f"{clock[4]} {clock[1]} {clock[2]}M", "%B %d, %Y %I:%M %p").replace(tzinfo=NY)
    if stamp.date() != archive_day or stamp.strftime("%A").lower() != clock[3].lower() or stamp > now:
        return None
    return period, stamp


def _publication(http, archive_day, code, heading, now, provider, failures):
    stem = f"{code}_{archive_day:%m%d%Y}"
    archive = "https://www.bls.gov/news.release/archives/" + stem
    pdf, article, dol = archive + ".pdf", archive + ".htm", DOL_INDEX + "/" + stem + ".pdf"
    for url in ((pdf, article, dol) if provider == "BLS" else (dol, pdf, article)):
        try:
            body = http.get(url)
            if not body:
                failures.append(f"{urlsplit(url).hostname}: empty release")
                continue
            if url.endswith(".pdf"):
                if not body.startswith(b"%PDF-") or len(body) > 10 * 1024 * 1024:
                    failures.append(f"{urlsplit(url).hostname}: unexpected PDF")
                    continue
                text = PdfReader(BytesIO(body)).pages[0].extract_text()
            else:
                page = BeautifulSoup(body, "html.parser", from_encoding="utf-8").find("pre")
                text = page.get_text(" ", strip=True) if page else ""
            clock = _release_clock(text, heading, archive_day, now)
            if clock:
                return clock[0], {"at": clock[1].isoformat(), "url": url}
            failures.append(f"{urlsplit(url).hostname}: unverified report header or embargo")
        except SourceError as exc:
            failures.append(str(exc))
        except (PyPdfError, IndexError, ValueError) as exc:
            failures.append(f"{urlsplit(url).hostname}: {type(exc).__name__}")
    return None


def verified_release(ctx, group, period, state, feeds, checked):
    if group not in PROGRAMS:
        return None
    cached = state.setdefault("publications", {})
    key = f"bls:{group}:{period}"
    if key in cached:
        return cached[key] if datetime.fromisoformat(cached[key]["at"]) <= ctx.now else None
    if (group, period) in feeds:
        return None
    code, heading = PROGRAMS[group]
    month = date.fromisoformat(period + "-01")
    end = month.replace(day=calendar.monthrange(month.year, month.month)[1])
    latest = min(end + timedelta(days=90), ctx.now.astimezone(NY).date())
    # The date only selects candidates. The actual report heading must match the period.
    attempted, failures = set(), []
    recent_candidate = False
    following_month = end + timedelta(days=1)
    today = ctx.now.astimezone(NY).date()
    for provider, days, error in _candidate_days(ctx, group, code, period, feeds):
        if error:
            failures.append(f"{provider} index: {error}")
        for day in [d for d in days if end < d <= latest][:3]:
            attempted.add(str(day))
            if today - day <= timedelta(days=2) and (
                provider == "calendar" or (day.year, day.month) == (following_month.year, following_month.month)
            ):
                recent_candidate = True
            if (code, day) not in checked:
                checked.add((code, day))
                entry = _publication(ctx.http, day, code, heading, ctx.now, provider, failures)
                if entry:
                    confirmed_key = f"bls:{group}:{entry[0]}"
                    cached[confirmed_key] = entry[1]
                    state.get("unverified_publications", {}).pop(confirmed_key, None)
            if key in cached:
                return cached[key]
    feeds[group, period] = None
    diagnoses = list(dict.fromkeys(failures))
    state.setdefault("unverified_publications", {})[key] = {
        "checked_at": ctx.now.isoformat(), "candidate_days": sorted(attempted), "errors": diagnoses[:8],
    }
    if recent_candidate and hasattr(ctx, "warn"):
        reason = diagnoses[-1] if diagnoses else "reference period or release header did not match"
        ctx.warn(f"BLS {group} {period}: values available, recent release clock unverified ({reason})")
    return None
