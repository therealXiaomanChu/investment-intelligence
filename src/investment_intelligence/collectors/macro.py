"""Official US macro release calendars and observed data vintages."""

from __future__ import annotations

import calendar
import copy
import csv
import io
import hashlib
import json
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from ..http import SourceError
from ..models import Fact, Record

NY = ZoneInfo("America/New_York")
UTC = timezone.utc
BLS = "https://www.bls.gov/schedule/news_release/bls.ics"
BLS_API = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
NY_FED_CALENDAR = "https://www.newyorkfed.org/research/calendars/"
IMF_ARC = "https://dsbb.imf.org/sdds-plus/country/USA/advance-release-calendar-base"
BEA = "https://www.bea.gov/news/schedule"
BEA_RELEASES = "https://www.bea.gov/news/current-releases"
FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv"
H15 = "https://www.federalreserve.gov/releases/h15/"
VIX_HISTORY = "https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
FED = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "macro"
MONTHS = {name.lower(): i for i in range(1, 13) for name in [calendar.month_name[i], calendar.month_abbr[i]]}
MONTH_YEAR = re.compile(r"\b(" + "|".join(MONTHS) + r")\.?\s+(20\d\d)\b", re.I)
BLS_GROUPS = {
    "consumer price index": "cpi",
    "producer price index": "ppi",
    "employment situation": "employment",
}


def _iso(value):
    return value.astimezone(UTC).isoformat()


def _period(text):
    match = MONTH_YEAR.search(text)
    if match:
        return f"{match[2]}-{MONTHS[match[1].lower()]:02d}"
    return None


def _previous(period):
    date_start = date.fromisoformat(period + "-01")
    return (date_start - timedelta(days=1)).strftime("%Y-%m")


def _macro_assets(ctx):
    return tuple(
        a["id"]
        for a in ctx.watchlist.get("assets", [])
        if a.get("symbol", "").upper() in {"SPY", "QQQ", "BTC", "ETH", "US2Y", "US10Y"}
    )


def _window(ctx, stamp):
    config = ctx.config("macro")
    before = config.get("revision_lookback_days", 90) if ctx.backfill else 2
    return (
        ctx.now - timedelta(days=before)
        <= stamp
        <= ctx.now + timedelta(days=config.get("calendar_horizon_days", 60))
    )


def _schedule(
    ctx,
    source,
    event_key,
    title,
    stamp,
    check_source,
    details,
    *,
    published_at=None,
    status="scheduled",
    evidence_id=None,
    evidence_url=None,
):
    metadata = {"check_source": check_source, **details}
    label = metadata.get("time_precision", "official")
    return Record(
        source_id=source["id"],
        external_id=evidence_id or event_key,
        url=evidence_url or source["url"],
        module="macro",
        kind="release",
        event_key=event_key,
        title=title,
        summary=f"{title} · {stamp.astimezone(NY):%Y-%m-%d %H:%M %Z} ({label} time, {status}).",
        event_at=_iso(stamp),
        published_at=published_at,
        status=status,
        scheduled_at=_iso(stamp),
        importance=0.45,
        assets=_macro_assets(ctx),
        metadata=metadata,
    )


def _ics_events(raw):
    text = re.sub(r"\r?\n[ \t]", "", raw.decode("utf-8-sig"))
    if "BEGIN:VCALENDAR" not in text or "END:VCALENDAR" not in text:
        raise ValueError("BLS calendar unavailable or iCalendar structure changed")
    for block in re.findall(r"BEGIN:VEVENT\s*(.*?)\s*END:VEVENT", text, re.S):
        fields = {}
        for line in block.splitlines():
            key, sep, value = line.partition(":")
            if sep:
                prop, _, parameters = key.upper().partition(";")
                fields[prop] = (value.strip().replace(r"\,", ",").replace(r"\n", " "), parameters)
        if "SUMMARY" not in fields or "DTSTART" not in fields:
            raise ValueError("BLS VEVENT missing SUMMARY or DTSTART")
        yield fields


def _ics_timestamp(field):
    value, params = field
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    if "TZID=" in params and not any(z in params for z in ("EASTERN", "AMERICA/NEW_YORK", "US/EASTERN")):
        raise ValueError("Unexpected BLS calendar time zone")
    return datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=NY)


def _bls_icalendar(ctx, source):
    matched = 0
    for fields in _ics_events(ctx.http.get(source["url"])):
        title = fields["SUMMARY"][0]
        details = title + " " + fields.get("DESCRIPTION", ("", ""))[0]
        group = next((group for name, group in BLS_GROUPS.items() if name in title.lower()), None)
        if not group:
            continue
        period = _period(details)
        if not period:
            # Without a verified reference period, the observation cannot be joined safely.
            ctx.warn("BLS release has no explicit reference month: " + title)
            continue
        matched += 1
        stamp = _ics_timestamp(fields["DTSTART"])
        if not _window(ctx, stamp):
            continue
        status = "canceled" if fields.get("STATUS", ("", ""))[0].upper() == "CANCELLED" else "scheduled"
        key = f"bls:{group}:{period}"
        yield _schedule(
            ctx,
            source,
            key,
            title,
            stamp,
            "bls_actuals",
            {"release_group": group, "period": period},
            status=status,
            evidence_id=fields.get("UID", (key, ""))[0],
        )
    if not matched:
        raise ValueError("BLS calendar contained no supported releases with reference months")


def _bls_program(ctx, source, group, label, url):
    raw = ctx.http.get(url)
    if not raw:
        raise ValueError("empty BLS program schedule")
    page = BeautifulSoup(raw, "html.parser")
    if not any(label.lower() in h.get_text(" ", strip=True).lower() for h in page.select("title,h1,h2")):
        raise ValueError("unexpected BLS program schedule title")
    for table in page.find_all("table"):
        rows = [r for r in table.find_all("tr") if r.find_parent("table") is table]
        if not rows:
            continue
        columns = [c.get_text(" ", strip=True).lower() for c in rows[0].find_all(["td", "th"])]
        if columns != ["reference month", "release date", "release time"]:
            continue
        if len(rows) == 1:
            raise ValueError("BLS program schedule has no rows")
        for row in rows[1:]:
            fields = [" ".join(c.stripped_strings) for c in row.find_all(["td", "th"], recursive=False)]
            if len(fields) != 3:
                ctx.warn(f"BLS {group} schedule has an unrecognized row")
                continue
            reference, release, clock = fields
            period = _period(reference)
            day = re.fullmatch(r"([a-z]+)\.?\s+(\d{1,2}),\s+(20\d\d)", release, re.I)
            month = MONTHS.get(day[1].lower()[:3], 0) if day else 0
            try:
                if not period or not month:
                    raise ValueError()
                stamp = datetime.combine(
                    date(int(day[3]), month, int(day[2])), datetime.strptime(clock, "%I:%M %p").time(), NY
                )
            except ValueError:
                ctx.warn(f"BLS {group} has no usable official date/time for {reference}")
                continue
            if _window(ctx, stamp):
                yield _schedule(
                    ctx, source, f"bls:{group}:{period}", f"{label} for {reference}", stamp, "bls_actuals",
                    {"release_group": group, "period": period}, evidence_url=url,
                )
        return
    raise ValueError("BLS program schedule columns unavailable")


def _bls_calendar(ctx, source):
    try:
        entries = list(_bls_icalendar(ctx, source))
    except (SourceError, ValueError) as primary:
        programs = {"cpi": "cpi", "ppi": "ppi", "employment": "empsit"}
        missing = set()
        for name, group in BLS_GROUPS.items():
            url = urljoin(BLS, programs[group] + ".htm")
            try:
                entries = list(_bls_program(ctx, source, group, name.title(), url))
            except (SourceError, ValueError):
                missing.add(group)
                continue
            yield from entries
        if missing:
            try:
                yield from _ny_fed_bls(ctx, source, missing)
            except (SourceError, ValueError) as exc:
                if missing == set(programs):
                    raise ValueError(f"{primary}; BLS programs and NY Fed calendar unavailable: {exc}") from primary
                ctx.warn(f"BLS program calendar unavailable for {', '.join(sorted(missing))}: {exc}")
        return
    yield from entries


def _imf_bls_periods(ctx):
    # This is the same public report request issued by the US SDDS Plus calendar.
    params = {"clientLocPath": IMF_ARC, "CountryCode": "USA", "countryType": "SDDSPlus",
              "CategoryCode": "ARC", "ContainerID": "SDDS_ARCWeekaheadlink"}
    data = ctx.http.get("https://dsbb.imf.org/api/report/getARCMetadataReportBase", params=params)
    if not data:
        raise ValueError("empty IMF release calendar")
    series = {"CPI00": "cpi", "EMP00": "employment", "PPI00": "ppi"}
    periods = {}
    rows = json.loads(data)
    if not isinstance(rows, list):
        raise ValueError("unexpected IMF release calendar response")
    for row in rows:
        group = series.get(row.get("CategoryCode"))
        if not group:
            continue
        for month, item in enumerate(row.get("MonthValues") or []):
            if not 1 <= month <= 12 or not item or not str(item.get("Day", "")).isdigit():
                continue
            match = re.fullmatch(r"([A-Za-z]{3})/(\d{2})", item.get("Period") or "")
            if not match or match[1].lower() not in MONTHS:
                continue
            ref_month, year = MONTHS[match[1].lower()], 2000 + int(match[2])
            release_year = year + int(month < ref_month)
            release = date(release_year, month, int(item["Day"]))
            periods[group, release] = f"{year}-{ref_month:02d}"
    return periods


def _ny_fed_bls(ctx, source, missing):
    try:
        periods = _imf_bls_periods(ctx)
    except (SourceError, ValueError, TypeError):
        periods = {}
    local = ctx.now.astimezone(NY).date()
    month = local.replace(day=1)
    horizon = local + timedelta(days=ctx.config("macro").get("calendar_horizon_days", 60))
    recovered, provisional = set(), set()
    errors = []
    while month <= horizon:
        next_month = (month + timedelta(days=32)).replace(day=1)
        url = NY_FED_CALENDAR + f"i-{month:%b%y}.html".lower()
        try:
            raw = ctx.http.get(url)
            page = BeautifulSoup(raw or b"", "html.parser")
            if not any(h.get_text(strip=True) == month.strftime("%B %Y") for h in page.select(".ts-data-table-head")):
                raise ValueError("month heading unavailable")
        except (SourceError, ValueError) as exc:
            errors.append(f"{month:%Y-%m} {exc}")
            month = next_month
            continue
        for cell in page.select("td.somatdR > div"):
            day = cell.find(string=True, recursive=False)
            if not day or not day.strip().isdigit():
                continue
            for anchor in cell.find_all("a", href=True):
                label = anchor.get_text(" ", strip=True)
                group = BLS_GROUPS.get(label.lower().removesuffix(" (ppi)"))
                if group not in missing or urlsplit(anchor["href"]).hostname != "www.bls.gov":
                    continue
                # The clock immediately after this link belongs to this release.
                clock = None
                for sibling in anchor.next_siblings:
                    if getattr(sibling, "name", None) not in (None, "br"):
                        break
                    if isinstance(sibling, str) and sibling.strip():
                        clock = re.fullmatch(r"\((\d{2}:\d{2})\)", sibling.strip())
                        break
                if not clock:
                    continue
                release = month.replace(day=int(day.strip()))
                stamp = datetime.combine(release, time.fromisoformat(clock[1]), NY)
                if not _window(ctx, stamp):
                    continue
                recovered.add(group)
                period = periods.get((group, release))
                meta = {"release_group": group, "period": period, "calendar_provider": "Federal Reserve Bank of New York"}
                if period:
                    meta["comparison_evidence"] = [IMF_ARC]
                    key = f"bls:{group}:{period}"
                else:
                    provisional.add(group)
                    key = f"bls-calendar:{group}:{release}"
                    label += " (reference month unconfirmed)"
                yield _schedule(ctx, source, key, label, stamp, "bls_actuals" if period else None,
                                meta, evidence_url=url, status="scheduled" if period else "estimated")
        month = next_month
    if not recovered:
        raise ValueError("No matching NY Fed schedule entries" + ("; " + " | ".join(errors) if errors else ""))
    if missing - recovered:
        ctx.warn("NY Fed schedule unavailable for " + ", ".join(sorted(missing - recovered)))
    if provisional:
        ctx.warn("NY Fed times verified; reference months pending independent confirmation for "
                 + ", ".join(sorted(provisional)))
    if errors:
        ctx.warn("NY Fed calendar coverage: " + " | ".join(errors))


def _bea_release_key(title):
    lower = title.lower()
    if "personal income and outlays" in lower and (period := _period(title)):
        return f"bea:pce:{period}"
    if not re.search(r"\b(gdp|gross domestic product)\b", lower):
        return None
    stage = re.search(r"\b(advance|second|third) estimate", lower)
    quarter = re.search(r"\b([1-4])(?:st|nd|rd|th) quarter[ ,]+(20\d\d)\b", lower)
    if stage and quarter:
        return f"bea:gdp:{quarter[2]}-Q{quarter[1]}:{stage[1]}"
    return None


def _bea_calendar(ctx, source):
    soup = BeautifulSoup(ctx.http.get(source["url"]), "html.parser")
    rows = soup.select("table#release-schedule-table tr, table:has(.release-title) tr")
    if not rows:
        raise ValueError("BEA schedule table unavailable or changed")
    year = None
    count = 0
    for row in rows:
        if header := row.find("th"):
            if match := re.search(r"\b(20\d\d)\b", header.get_text(" ", strip=True)):
                year = int(match[1])
        cell = row.select_one(".release-title")
        if cell is None:
            continue
        count += 1
        title = cell.get_text(" ", strip=True)
        key = _bea_release_key(title)
        if not key:
            continue
        date_node, time_node = row.select_one(".release-date"), row.select_one(".scheduled-date small")
        if year is None or not date_node or not time_node:
            raise ValueError("BEA target release missing date, time or year")
        stamp = datetime.strptime(
            f"{year} {date_node.get_text(strip=True)} {time_node.get_text(strip=True)}", "%Y %B %d %I:%M %p"
        ).replace(tzinfo=NY)
        if _window(ctx, stamp):
            yield _schedule(
                ctx,
                source,
                key,
                title,
                stamp,
                "bea_releases",
                {"release_group": key.split(":")[1], "period": key.split(":")[2]},
            )
    if not count:
        raise ValueError("BEA schedule table has no release rows")


def _due(ctx, source):
    targets = []
    oldest = ctx.now - timedelta(days=ctx.config("macro").get("revision_lookback_days", 90))
    for record in ctx.due:
        if record["metadata"].get("check_source") != source["id"]:
            continue
        stamp = record.get("scheduled_at")
        if not stamp and source["adapter"] == "bls_actuals" and ctx.job == "calendar_sync":
            stamp = record.get("event_at")
            if stamp and datetime.fromisoformat(stamp) < oldest:
                continue
        if stamp and datetime.fromisoformat(stamp) <= ctx.now:
            targets.append(record)
    return targets


def _link(base, href):
    url = urljoin(base, href)
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != urlsplit(base).hostname:
        raise ValueError("Release page points outside the official source")
    return url


def _bea_table(body, key, vintage):
    parts = key.split(":")
    group, period = parts[1:3]
    soup = BeautifulSoup(body, "html.parser")
    for table in soup.find_all("table"):
        rows = [[c.get_text(" ", strip=True) for c in r.find_all(["th", "td"])] for r in table.find_all("tr")]
        if len(rows) < 3 or not rows[0]:
            continue
        description = rows[0][0].lower()
        headers = [h.lower() for h in rows[1][1:]]
        if group == "pce" and "percent change from preceding month" in description:
            expected = MONTHS.get(headers[-1]) if headers else None
            if expected != int(period[-2:]):
                continue
            periods = [(_previous(period), "previous"), (period, "actual")]
            if not headers or len(headers) != 2 or MONTHS.get(headers[0]) != int(_previous(period)[-2:]):
                continue
            unit = "percent change from preceding month"
        elif group == "gdp" and "percent change (saar)" in description:
            if not headers or (parts[3] + " estimate") != headers[-1]:
                continue
            if "to " + period.replace("-", ":").lower() not in description:
                continue
            periods = [(period, h) for h in headers]
            unit = "percent change, seasonally adjusted annual rate"
        else:
            continue
        facts = []
        for row in rows[2:]:
            label = row[0].lower().replace(",", "") if row else ""
            if label not in {"real gdp", "pce price index", "pce price index excluding food and energy"}:
                continue
            if len(row) != len(periods) + 1:
                continue
            for text, (reference, kind) in zip(row[1:], periods):
                text = text.replace("−", "-")
                if re.fullmatch(r"-?\d+(?:\.\d+)?", text):
                    facts.append(Fact(f"{row[0]} · {kind}", text, unit, reference, vintage))
        return tuple(facts)
    return ()


def _bea_published(ctx, source):
    due = {d["event_key"]: d for d in _due(ctx, source)}
    discover = ctx.job == "calendar_sync" and not due
    if not due and not discover:
        return
    soup = BeautifulSoup(ctx.http.get(source["url"]), "html.parser")
    rows = soup.select("tr.release-row")
    if not rows:
        raise ValueError("BEA published release index unavailable or changed")
    for row in rows:
        anchor, timestamp = row.select_one("td a[href]"), row.select_one("time[datetime]")
        if not anchor or not timestamp:
            raise ValueError("Missing published title or date in BEA index")
        title = anchor.get_text(" ", strip=True)
        key = _bea_release_key(title)
        if not key or (not discover and key not in due):
            continue
        stamp = datetime.fromisoformat(timestamp["datetime"])
        if stamp.tzinfo is None or stamp > ctx.now or (discover and ctx.now - stamp > timedelta(days=90)):
            continue
        url = _link(source["url"], anchor["href"])
        facts = _bea_table(ctx.http.get(url), key, _iso(stamp))
        summary = f"BEA published {title}."
        if facts:
            metric = "Real GDP" if ":gdp:" in key else "PCE price index"
            latest = [
                f for f in facts if f.metric.split(" · ")[0] == metric and f.period == key.split(":")[2]
            ]
            if latest:
                summary += f" {metric}: {latest[-1].value} {latest[-1].unit}."
        else:
            ctx.warn("BEA structured release measures unavailable for " + key)
        yield Record(
            source_id=source["id"],
            external_id=key,
            url=url,
            module="macro",
            kind="release",
            event_key=key,
            title=title,
            summary=summary,
            facts=facts,
            event_at=_iso(stamp),
            published_at=_iso(stamp),
            importance=0.9,
            scheduled_at=due.get(key, {}).get("scheduled_at"),
            metadata=due.get(key, {}).get("metadata") or {
                "release_group": key.split(":")[1], "period": key.split(":")[2], "check_source": source["id"],
            },
            assets=_macro_assets(ctx),
        )


def _fed_calendar(ctx, source):
    due = {d["event_key"] for d in _due(ctx, source)}
    only_due = getattr(ctx, "job", "") == "event_check"
    if only_due and not due:
        return
    soup = BeautifulSoup(ctx.http.get(source["url"]), "html.parser")
    meetings = soup.select(".fomc-meeting")
    if not meetings:
        raise ValueError("FOMC meeting calendar unavailable or changed")
    for meeting in meetings:
        panel = meeting.find_parent(class_="panel")
        header = panel.find("h4") if panel else None
        heading = re.search(r"(20\d\d) FOMC Meetings", header.get_text(" ", strip=True)) if header else None
        if not heading:
            raise ValueError("FOMC meeting year unavailable")
        year = int(heading[1])
        if abs(year - ctx.now.year) > 1:
            continue
        month, day = meeting.select_one(".fomc-meeting__month"), meeting.select_one(".fomc-meeting__date")
        if not month or not day:
            raise ValueError("FOMC meeting date unavailable")
        month_text = month.get_text(strip=True).split("/")[-1].lower().strip(".")
        # Special conference calls and notation votes have no regular 14:00 release assumption.
        day_text = day.get_text(strip=True)
        match = re.fullmatch(r"\s*\d{1,2}\s*[-–]\s*(\d{1,2})\*?\s*", day_text)
        if not match or month_text not in MONTHS:
            continue
        stamp = datetime.combine(date(year, MONTHS[month_text], int(match[1])), time(14), NY)
        meeting_day = stamp.strftime("%Y%m%d")
        minutes_stamp = stamp + timedelta(weeks=3)
        minute_cell = meeting.select_one(".fomc-meeting__minutes")
        if minute_cell:
            text = minute_cell.get_text(" ", strip=True)
            explicit = re.search(r"Released ([A-Z][a-z]+ \d{1,2}, 20\d{2})", text)
            if explicit:
                minutes_day = datetime.strptime(explicit[1], "%B %d, %Y").date()
                minutes_stamp = datetime.combine(minutes_day, time(14), NY)
        releases = [
            ("fomc", "FOMC policy statement", stamp, f"/newsevents/pressreleases/monetary{meeting_day}a.htm"),
            (
                "minutes",
                "FOMC meeting minutes",
                minutes_stamp,
                f"/monetarypolicy/fomcminutes{meeting_day}.htm",
            ),
        ]
        if "*" in day_text:
            releases.append(
                (
                    "sep",
                    "FOMC Summary of Economic Projections",
                    stamp,
                    f"/monetarypolicy/fomcprojtabl{meeting_day}.htm",
                )
            )
        for group, title, release_at, document_path in releases:
            key = f"fed:{group}:{stamp:%Y-%m-%d}"
            if (only_due and key not in due) or (not _window(ctx, release_at) and key not in due):
                continue
            details = {
                "release_group": group,
                "time_precision": "estimated",
                "meeting_end_date": stamp.date().isoformat(),
                "sep_meeting": "*" in day_text,
            }
            if not only_due:
                yield _schedule(
                    ctx, source, key, title, release_at, source["id"], details, evidence_id="calendar:" + key
                )
            anchor = meeting.find("a", href=document_path)
            if anchor is None or release_at > ctx.now:
                continue
            url = _link(source["url"], anchor["href"])
            yield Record(
                source_id=source["id"],
                external_id="document:" + key,
                url=url,
                module="macro",
                kind="release",
                event_key=key,
                title=title,
                summary=f"The Federal Reserve posted {title} for the meeting ending {stamp:%Y-%m-%d}.",
                event_at=_iso(release_at),
                published_at=None,
                scheduled_at=_iso(release_at),
                assets=_macro_assets(ctx),
                importance=0.92,
                metadata=details,
            )


def _observations(body):
    response = json.loads(body)
    if response.get("status") != "REQUEST_SUCCEEDED":
        raise ValueError("BLS data unavailable: " + " ".join(map(str, response.get("message", [])))[:240])
    rows = response.get("Results", {}).get("series")
    if not isinstance(rows, list) or not rows:
        raise ValueError("BLS response missing series")
    observations = {}
    for series in rows:
        values = {}
        for row in series["data"]:
            # M13 (annual average) is not a month.
            if not re.fullmatch(r"M(0[1-9]|1[0-2])", row["period"]):
                continue
            if row["value"] in {"-", "", "N/A"}:
                # Explicit BLS missing value, including the October 2025 shutdown.
                continue
            value = Decimal(row["value"])
            if not value.is_finite():
                raise ValueError("BLS observation is not finite")
            values[f'{int(row["year"]):04d}-{row["period"][1:]}'] = str(value)
        observations[series["seriesID"]] = values
    return observations


def _level_facts(metrics, period, observations, state):
    """Compare only matching series, explicit adjacent months and observed baselines."""
    facts = []
    for spec in metrics:
        series, unit = spec["id"], spec["unit"]
        current = observations[series].get(period)
        earlier = _previous(period)
        previous = observations[series].get(earlier)
        base = state["originals"].get(f"{series}/{earlier}", previous)
        field = state["originals"].setdefault(f"{series}/{period}", current)
        # Store a previous month's observed value so its subsequent revisions are detectable.
        if previous is not None:
            state["originals"].setdefault(f"{series}/{earlier}", previous)
        revision = str(Decimal(current) - Decimal(field)) if Decimal(current) != Decimal(field) else None
        revised_prev = (
            previous
            if previous is not None and base is not None and Decimal(previous) != Decimal(base)
            else None
        )
        values = [
            ("actual", current, period, unit),
            ("previous", base, earlier, unit),
            ("revised_previous", revised_prev, earlier, unit),
            ("revision", revision, period, unit),
        ]
        if previous is not None:
            change = Decimal(current) - Decimal(previous)
            values.append(("change", str(change), period, unit))
            if series.startswith(("CU", "WP")) and Decimal(previous) != 0:
                pct = (100 * change / Decimal(previous)).quantize(Decimal("0.001"))
                values.append(("month_over_month", str(pct), period, "percent change"))
        facts.extend((f'{spec["name"]} · {metric}', val, units, p) for metric, val, p, units in values)
    return facts


def _bls_transition(ctx, state, group, latest):
    """A baseline confirms a period advanced. The API does not supply a release timestamp."""
    if ctx.backfill:
        return None
    checks = state.setdefault("latest_checks", {})
    prior = checks.get(group)
    if prior and latest < prior["period"]:
        return None
    expected = _previous(ctx.now.astimezone(NY).strftime("%Y-%m"))
    advance = False
    if prior and latest == expected and prior["period"] == _previous(latest):
        age = ctx.now - datetime.fromisoformat(prior["checked_at"])
        advance = timedelta(0) <= age <= timedelta(days=1)
    # Daily collection can discover a new period before the alert window. Keep the
    # prior baseline so the next scheduled probe can confirm and alert on the transition.
    if not advance or ctx.job == "bls_probe":
        checks[group] = {"period": latest, "checked_at": _iso(ctx.now)}
    if not advance or ctx.job != "bls_probe":
        return None
    vintage = state.get("snapshots", {}).get(f"bls:{group}:{latest}", {}).get("vintage", _iso(ctx.now))
    return vintage if datetime.fromisoformat(vintage) >= datetime.fromisoformat(prior["checked_at"]) else None


def _bls_actuals(ctx, source):
    targets = _due(ctx, source)
    probe = ctx.job == "bls_probe"
    discover = ctx.job == "calendar_sync" or probe
    if not targets and not discover:
        return
    groups = ctx.config("macro")["bls_series"]
    active_groups = set(groups) if discover else {d["metadata"]["release_group"] for d in targets}
    needed = sorted({spec["id"] for g in active_groups for spec in groups[g]})
    years = [int(d["metadata"]["period"][:4]) for d in targets] + [ctx.now.year]
    query = {"seriesid": needed, "startyear": str(min(years) - 1), "endyear": str(max(years))}
    if token := ctx.env.get("BLS_API_KEY"):
        query["registrationkey"] = token
    observations = _observations(ctx.http.post_json(source["url"], query))
    if any(series not in observations for series in needed):
        raise ValueError("BLS response missing one or more requested series")
    # Bootstrap when the calendar is inaccessible. The level is observable from BLS itself;
    # its reference month is known and its actual publication timestamp is unknown.
    known = {d["event_key"] for d in targets}
    state = copy.deepcopy(ctx.cursor)
    if discover:
        for group, specs in groups.items():
            months = set.intersection(*(set(observations[spec["id"]]) for spec in specs))
            if not months:
                ctx.warn(f"BLS {group} has no common reference period")
                continue
            last = max(months)
            observed = _bls_transition(ctx, state, group, last)
            if probe:
                if observed is None:
                    continue
                key = f"bls:{group}:{last}"
                if key not in known:
                    state.setdefault("observed_updates", {})[key] = observed
            floor = (ctx.now.date().replace(day=1) - timedelta(days=90)).strftime("%Y-%m")
            if last < floor:
                ctx.warn(f"BLS {group} latest reference period is stale: {last}")
            # Backfill all reference months in the configured observation overlap.
            periods = sorted(m for m in months if m >= floor) if ctx.backfill else [last]
            for period in periods:
                key = f"bls:{group}:{period}"
                if key not in known:
                    targets.append(
                        {
                            "event_key": key,
                            "scheduled_at": None,
                            "metadata": {
                                "check_source": source["id"],
                                "period": period,
                                "release_group": group,
                            },
                        }
                    )
    targets.sort(key=lambda d: d["metadata"]["period"])
    state.setdefault("originals", {})
    state.setdefault("snapshots", {})
    for d in targets:
        meta = {k: d["metadata"][k] for k in ("check_source", "period", "release_group")}
        key, period, group = d["event_key"], meta["period"], meta["release_group"]
        specs = groups[group]
        if any(period not in observations[s["id"]] for s in specs):
            # The API sometimes still serves last month's release; let event_check retry.
            ctx.warn(f"BLS {group} observations unavailable for {period}")
            continue
        raw = _level_facts(specs, period, observations, state)
        fingerprint = hashlib.sha256(json.dumps(raw, ensure_ascii=False).encode()).hexdigest()
        snapshot = state["snapshots"].get(key, {})
        if snapshot.get("hash") != fingerprint:
            snapshot = {"hash": fingerprint, "vintage": _iso(ctx.now)}
            state["snapshots"][key] = snapshot
        facts = tuple(Fact(metric, value, unit, p, snapshot["vintage"]) for metric, value, unit, p in raw)
        # Readers can inspect the BLS batch members individually with a keyless series URL.
        primary = specs[0]
        urls = {s["id"]: BLS_API + s["id"] for s in specs}
        meta.update(
            {
                "series_urls": urls,
                "consensus": None,
                "consensus_as_of": None,
                "provider": None,
                "vintage_basis": "first observed snapshot",
            }
        )
        first = facts[0].value
        title = f'{primary["name"]} · {period}'
        summary = f'{primary["name"]} was {first} {primary["unit"]} in {period}.'
        if facts[3].value is not None:
            summary += f" Revision from first observed level: {facts[3].value}."
        stamp = d.get("scheduled_at") or state.get("release_times", {}).get(key)
        if stamp:
            state.setdefault("release_times", {})[key] = stamp
            event_at = stamp
        elif observed := state.get("observed_updates", {}).get(key):
            event_at = observed
            meta["time_precision"] = "first observed via BLS API; release time unavailable"
            summary += f" First observed via the BLS API at {observed}; release time unavailable."
        else:
            # The dated event is the end of the underlying survey month, explicitly labeled.
            start = date.fromisoformat(period + "-01")
            month_end = start.replace(day=calendar.monthrange(start.year, start.month)[1])
            event_at = _iso(datetime.combine(month_end, time.min, NY))
            meta["time_precision"] = "reference month end; release time unavailable"
        yield Record(
            source_id=source["id"],
            external_id=key,
            url=urls[primary["id"]],
            module="macro",
            kind="release",
            event_key=key,
            title=title,
            summary=summary,
            event_at=event_at,
            published_at=None,
            status="published",
            scheduled_at=stamp,
            assets=_macro_assets(ctx),
            facts=facts,
            importance=0.95,
            metadata=meta,
        )
    ctx.next_cursor.update(state)


def _fred_series(ctx, source, series, start, end):
    if key := ctx.env.get("FRED_API_KEY"):
        # Do not store this key-bearing request URL as evidence or in the source cursor.
        query = {
            "series_id": series,
            "api_key": key,
            "file_type": "json",
            "observation_start": str(start),
            "observation_end": str(end),
        }
        data = json.loads(ctx.http.get("https://api.stlouisfed.org/fred/series/observations", params=query))
        if not isinstance(data.get("observations"), list):
            raise ValueError(f"Unexpected FRED {series} observations response")
        for row in data["observations"]:
            yield row["date"], row["value"]
        return
    body = ctx.http.get(source["url"], params={"id": series, "cosd": str(start), "coed": str(end)})
    reader = csv.DictReader(io.StringIO(body.decode("utf-8-sig")))
    if reader.fieldnames != ["observation_date", series]:
        raise ValueError(f"Unexpected FRED {series} export columns")
    for row in reader:
        yield row["observation_date"], row[series]


def _h15_nominal(raw):
    table = BeautifulSoup(raw, "html.parser").select_one("table#h15table")
    if table is None:
        raise ValueError("Federal Reserve H.15 table unavailable")
    dates = {
        th["id"]: datetime.strptime(th.get_text(" ", strip=True), "%Y %b %d").date().isoformat()
        for th in table.select("th.colhead[id]") if re.fullmatch(r"col\d+", th["id"])
    }
    labels = {th["id"]: th.get_text(" ", strip=True) for th in table.select("th[id]")}
    observations = {}
    for row in table.find_all("tr"):
        heading = row.find("th")
        if not heading:
            continue
        series = {"2-year": "DGS2", "10-year": "DGS10"}.get(heading.get_text(strip=True))
        parents = [labels.get(i, "") for i in heading.get("headers", [])]
        if not series or "Treasury constant maturities" not in parents or not any(
            re.fullmatch(r"Nominal(?:\s+\d+)?", label) for label in parents
        ):
            continue
        values = []
        for cell in row.find_all("td"):
            day = next((dates[i] for i in cell.get("headers", []) if i in dates), None)
            if not day:
                raise ValueError("Federal Reserve H.15 observation has no trading date")
            value = cell.get_text(strip=True)
            if value.upper() not in {"", "N.A.", "N/A", "ND", "--"}:
                values.append((day, value))
        observations[series] = values
    if not dates or set(observations) != {"DGS2", "DGS10"}:
        raise ValueError("Federal Reserve H.15 nominal yield columns unavailable")
    return observations


def _vix_closes(raw):
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    if reader.fieldnames != ["DATE", "OPEN", "HIGH", "LOW", "CLOSE"]:
        raise ValueError("Unexpected Cboe VIX history columns")
    return [(datetime.strptime(row["DATE"], "%m/%d/%Y").date().isoformat(), row["CLOSE"]) for row in reader]


def _fred_market(ctx, source):
    specs = {
        "DGS2": ("2-year Treasury yield", "percent", "US2Y"),
        "DGS10": ("10-year Treasury yield", "percent", "US10Y"),
        "VIXCLS": ("Cboe VIX close", "index points", "VIX"),
    }
    today = ctx.now.astimezone(NY).date()
    cursor = ctx.cursor.get("through")
    overlap = 10
    if ctx.backfill:
        start = today - timedelta(days=ctx.config("macro").get("revision_lookback_days", 90))
    else:
        start = (date.fromisoformat(cursor) if cursor else today) - timedelta(days=overlap)
    # Requests use a bounded date range and replay the overlap for revised market closes.
    end = min(today, start + timedelta(days=100))
    observations = {}
    policy = ctx.config("macro").get("market_provider", "auto")
    if policy not in {"auto", "publishers"}:
        raise ValueError("market_provider must be auto or publishers")
    use_fred = policy == "auto"
    publisher_data = {}
    urls = {}
    for series in specs:
        if use_fred:
            try:
                values = list(_fred_series(ctx, source, series, start, end))
                urls[series] = "https://fred.stlouisfed.org/series/" + series
            except SourceError:
                # An unavailable FRED endpoint would otherwise consume three retry cycles per series.
                use_fred = False
        if not use_fred:
            url = VIX_HISTORY if series == "VIXCLS" else H15
            if url not in publisher_data:
                try:
                    raw = ctx.http.get(url)
                    publisher_data[url] = {"VIXCLS": _vix_closes(raw)} if url == VIX_HISTORY else _h15_nominal(raw)
                except (SourceError, ValueError) as exc:
                    ctx.warn(f"Official market source {url} unavailable: {exc}")
                    publisher_data[url] = {}
            if series not in publisher_data[url]:
                continue
            values = publisher_data[url][series]
            urls[series] = url
        recent = False
        for observed_on, raw in values:
            day = date.fromisoformat(observed_on)
            if raw in (None, "", ".") or not start <= day <= end:
                continue
            value = Decimal(raw)
            if not value.is_finite():
                raise ValueError("Nonfinite FRED observation")
            observations.setdefault(day, {})[series] = str(value)
            recent |= end - day <= timedelta(days=7)
        if not recent:
            ctx.warn(f"{series} has no observation within 7 days of {end}")
    if not urls:
        raise ValueError("All official market data sources unavailable: " + " | ".join(ctx.warnings))
    by_symbol = {a["symbol"]: a["id"] for a in ctx.watchlist["assets"]}
    snapshots = copy.deepcopy(ctx.cursor.get("snapshots", {}))
    # Whole dates commit together. A short page is resumed from its last complete date.
    emitted = 0
    through = cursor
    for day, values in sorted(observations.items()):
        if emitted >= ctx.limit:
            break
        key = "fred:market:" + str(day)
        signature = sorted(values.items())
        previous = snapshots.get(str(day), {})
        if not previous.keys() <= values.keys() or (
            previous.keys() == values.keys() and all(Decimal(previous[s]) == Decimal(v) for s, v in signature)
        ):
            # Shrinking source coverage must not erase an archived fact. Different display precision
            # across distributors alone is not a revision.
            through = str(day)
            continue
        facts = []
        assets = []
        evidence = {}
        for series, value in signature:
            name, unit, symbol = specs[series]
            facts.append(Fact(name, value, unit, str(day), _iso(ctx.now)))
            if symbol in by_symbol:
                assets.append(by_symbol[symbol])
            evidence[series] = urls[series]
        if "DGS2" in values and "DGS10" in values:
            spread = 100 * (Decimal(values["DGS10"]) - Decimal(values["DGS2"]))
            facts.append(
                Fact(
                    "10-year minus 2-year Treasury spread",
                    str(spread),
                    "basis points",
                    str(day),
                    _iso(ctx.now),
                )
            )
        summary = ", ".join(f"{s} {val}" for s, val in signature) + f" on {day}."
        primary = signature[0][0]
        yield Record(
            source_id=source["id"],
            external_id=key,
            url=evidence[primary],
            module="macro",
            kind="market_observation",
            event_key=key,
            title=f"Market observations · {day}",
            summary=summary,
            event_at=_iso(datetime.combine(day, time.min, NY)),
            published_at=None,
            assets=tuple(assets),
            facts=tuple(facts),
            importance=0.45,
            metadata={"time_precision": "observation date only", "series_urls": evidence},
        )
        snapshots[str(day)] = dict(signature)
        through = str(day)
        emitted += 1
    if emitted < ctx.limit and observations:
        through = str(end)
    if through:
        ctx.next_cursor["through"] = max(through, cursor or through)
    # Keep revision fingerprints across ordinary overlap and explicit 90-day backfills.
    cutoff = str(end - timedelta(days=ctx.config("macro").get("revision_lookback_days", 90) + overlap))
    ctx.next_cursor["snapshots"] = {d: v for d, v in snapshots.items() if d >= cutoff}


def collect(ctx, source):
    """Return records from one source, leaving transactions and retry ownership to the runner."""
    handlers = {
        "bls_calendar": _bls_calendar,
        "bea_calendar": _bea_calendar,
        "fed_calendar": _fed_calendar,
        "bls_actuals": _bls_actuals,
        "bea_releases": _bea_published,
        "fred_market": _fred_market,
    }
    yield from handlers[source["adapter"]](ctx, source)


def demo_response(url, method, payload, phase):
    files = {BLS: "schedule.ics", BEA: "bea.html", FED: "fed.html", BEA_RELEASES: "releases.html"}
    if url.rstrip("/") == BLS_API.rstrip("/") and method.upper() == "POST":
        return (FIXTURES / f"bls-{min(phase, 2)}.json").read_bytes()
    if url.split("?")[0] == FRED:
        series = parse_qs(urlsplit(url).query).get("id", [None])[0]
        if series in {"DGS2", "DGS10", "VIXCLS"}:
            return (FIXTURES / f"{series}.csv").read_bytes()
    if url == "https://www.bea.gov/news/2026/demo-personal-income-september-2026":
        return (FIXTURES / "pce-release.html").read_bytes()
    if url in files:
        return (FIXTURES / files[url]).read_bytes()
    return None
