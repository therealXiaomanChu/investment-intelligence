"""Verify BLS release clocks against the dated publication itself."""

import calendar
from datetime import date, datetime, timedelta
from io import BytesIO
import re
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


def _archive_links(http, code):
    raw = http.get(f"https://www.bls.gov/feed/{code}.rss")
    if not raw:
        return []
    feed = fromstring(raw)
    if feed.tag != ATOM + "feed" or feed.findtext(ATOM + "id") != f"bls.gov:feed:{code}":
        return []
    entries = []
    for link in feed.findall(ATOM + "entry/" + ATOM + "link"):
        url = link.get("href", "")
        match = re.fullmatch(rf"https://www\.bls\.gov/news\.release/archives/{code}_(\d{{8}})\.htm", url)
        if match:
            entries.append((datetime.strptime(match[1], "%m%d%Y").date(), url))
    return sorted(set(entries))


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


def _publication(http, archive_day, article, heading, now):
    pdf = article.removesuffix(".htm") + ".pdf"
    # Both the bureau and its parent department distribute the dated release PDF.
    dol = "https://www.dol.gov/newsroom/economicdata/" + pdf.rsplit("/", 1)[-1]
    for url in (pdf, article, dol):
        try:
            body = http.get(url)
            if not body:
                continue
            if url.endswith(".pdf"):
                if not body.startswith(b"%PDF-") or len(body) > 10 * 1024 * 1024:
                    continue
                text = PdfReader(BytesIO(body)).pages[0].extract_text()
            else:
                page = BeautifulSoup(body, "html.parser", from_encoding="utf-8").find("pre")
                text = page.get_text(" ", strip=True) if page else ""
            clock = _release_clock(text, heading, archive_day, now)
            if clock:
                return clock[0], {"at": clock[1].isoformat(), "url": url}
        except (SourceError, PyPdfError, IndexError, ValueError):
            continue
    return None


def verified_release(ctx, group, period, state, feeds, checked):
    if group not in PROGRAMS:
        return None
    cached = state.setdefault("publications", {})
    key = f"bls:{group}:{period}"
    if key in cached:
        return cached[key]
    code, heading = PROGRAMS[group]
    if group not in feeds:
        try:
            feeds[group] = _archive_links(ctx.http, code)
        except (SourceError, ParseError, ValueError):
            feeds[group] = []
    month = date.fromisoformat(period + "-01")
    end = month.replace(day=calendar.monthrange(month.year, month.month)[1])
    latest = min(end + timedelta(days=90), ctx.now.astimezone(NY).date())
    # The date only selects candidates. The actual report heading must match the period.
    candidates = [(day, url) for day, url in feeds[group] if end < day <= latest]
    for day, url in candidates[:3]:
        if url not in checked:
            checked.add(url)
            entry = _publication(ctx.http, day, url, heading, ctx.now)
            if entry:
                cached[f"bls:{group}:{entry[0]}"] = entry[1]
        if key in cached:
            return cached[key]
    return None
