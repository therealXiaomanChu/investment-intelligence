from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
import yaml

from investment_intelligence.collectors import macro
from investment_intelligence.models import utc
from investment_intelligence.storage import Database

CONFIG = Path(__file__).resolve().parents[1] / "config"
NOW = datetime(2026, 11, 2, 14, 10, tzinfo=timezone.utc)


class Transport:
    def __init__(self):
        self.phase = 0
        self.overrides = {}
        self.calls = []

    def get(self, url, params=None, headers=None):
        target = url + ("?" + urlencode(params) if params else "")
        self.calls.append(target)
        if url in self.overrides:
            return self.overrides[url]
        return macro.demo_response(target, "GET", None, self.phase)

    def post_json(self, url, payload, headers=None):
        self.calls.append(url)
        return self.overrides.get(url, macro.demo_response(url, "POST", payload, self.phase))


@pytest.fixture
def ctx():
    notes = []
    return SimpleNamespace(
        now=NOW,
        http=Transport(),
        job="calendar_sync",
        limit=100,
        config=lambda key: yaml.safe_load((CONFIG / (key + ".yaml")).read_text()),
        watchlist=yaml.safe_load((CONFIG / "watchlist.example.yaml").read_text()),
        cursor={},
        next_cursor={},
        backfill=False,
        due=[],
        env={},
        warn=notes.append,
        warnings=notes,
    )


def source(name, url):
    return {"id": name, "adapter": name, "module": "macro", "url": url}


def records(ctx, src):
    items = list(macro.collect(ctx, src))
    ctx.cursor = ctx.next_cursor
    ctx.next_cursor = ctx.cursor.copy()
    return items


def test_schedule_then_stale_then_publication_and_real_revision(ctx, tmp_path):
    calendar = source("bls_calendar", macro.BLS)
    actuals = source("bls_actuals", macro.BLS_API)
    db = Database("sqlite:///" + str(tmp_path / "macro.sqlite"))
    db.register(ctx.watchlist["assets"], [calendar, actuals])
    ctx.now = NOW - timedelta(hours=2)
    [release] = records(ctx, calendar)
    with db.write() as conn:
        assert db.ingest(conn, release, utc(ctx.now))
    assert release.status == "scheduled" and release.scheduled_at == "2026-11-02T13:30:00.000000+00:00"
    ctx.due = [release.payload()]
    ctx.job = "event_check"
    assert records(ctx, actuals) == []
    assert macro.BLS_API not in ctx.http.calls
    ctx.now = NOW
    assert records(ctx, actuals) == []
    assert "unavailable" in ctx.warnings[-1]
    ctx.http.phase = 1
    [initial] = records(ctx, actuals)
    with db.write() as conn:
        assert db.ingest(conn, initial, utc(ctx.now))
        assert not db.ingest(conn, initial, utc(ctx.now))
    ctx.now += timedelta(days=1)
    [unchanged] = records(ctx, actuals)
    assert unchanged == initial
    ctx.http.phase = 2
    [revised] = records(ctx, actuals)
    fields = {f.metric: f for f in revised.facts}
    first = "CPI-U all items · "
    assert fields[first + "actual"].value == "304.300"
    assert fields[first + "previous"].value == "302.000"
    assert fields[first + "revised_previous"].value == "302.500"
    assert fields[first + "revision"].value == "0.300"
    assert fields[first + "month_over_month"].value == "0.595"
    assert revised.metadata["consensus"] is None and "surprise" not in revised.summary.lower()
    with db.write() as conn:
        assert db.ingest(conn, revised, utc(ctx.now))
        assert not db.ingest(conn, release, utc(ctx.now))
    assert len(db.all("SELECT * FROM events")) == 1
    assert db.all("SELECT status FROM events") == [{"status": "published"}]
    assert [
        r["value"]
        for r in db.all("SELECT value FROM facts WHERE metric=:m ORDER BY vintage", m=first + "actual")
    ] == ["304.000", "304.300"]
    assert len(db.all("SELECT DISTINCT canonical_url FROM documents")) == 2


def test_bls_ics_folding_utc_revision_identity_and_cancel(ctx):
    src = source("bls_calendar", macro.BLS)
    [original] = records(ctx, src)
    revised = (
        ctx.http.get(macro.BLS)
        .decode()
        .replace(
            "DTSTART;TZID=America/New_York:20261102T083000", "DTSTART:20261102T160000Z\nSTATUS:CANCELLED"
        )
        .replace("for October", "for Oc\n tober")
    )
    ctx.http.overrides[macro.BLS] = revised.encode()
    [changed] = records(ctx, src)
    assert original.event_key == changed.event_key and original.external_id == changed.external_id
    assert changed.status == "canceled" and changed.scheduled_at.startswith("2026-11-02T16:00")


def test_bls_refuses_unlabeled_reference_period_and_changed_response(ctx):
    src = source("bls_calendar", macro.BLS)
    ctx.http.overrides[macro.BLS] = ctx.http.get(macro.BLS).replace(b" for October 2026", b"")
    with pytest.raises(ValueError, match="no supported releases"):
        records(ctx, src)
    with pytest.raises(ValueError, match="missing series"):
        macro._observations(b'{"status":"REQUEST_SUCCEEDED","Results":{}}')
    with pytest.raises(ValueError, match="unavailable"):
        macro._observations(b'{"status":"REQUEST_NOT_PROCESSED","message":["quota"]}')


def test_bea_period_and_estimate_stage_must_match(ctx):
    ctx.backfill = True
    calendar = source("bea_calendar", macro.BEA)
    releases = source("bea_releases", macro.BEA_RELEASES)
    schedules = records(ctx, calendar)
    assert len(schedules) == 2
    ctx.due = [s.payload() for s in schedules]
    ctx.now = datetime(2026, 11, 26, tzinfo=timezone.utc)
    assert records(ctx, releases) == []
    index = ctx.http.get(macro.BEA_RELEASES).decode().replace("Outlays, September", "Outlays, October")
    index = index.replace("2026-10-29T08:30:00-04:00", "2026-11-25T08:30:00-05:00")
    ctx.http.overrides[macro.BEA_RELEASES] = index.encode()
    [matched] = records(ctx, releases)
    assert matched.event_key == "bea:pce:2026-10" and matched.status == "published"
    assert [(f.value, f.period) for f in matched.facts[:2]] == [("0.2", "2026-09"), ("0.3", "2026-10")]
    assert macro._bea_release_key("GDP (Third Estimate), 3rd Quarter 2026") != schedules[0].event_key
    ctx.http.overrides[macro.BEA] = b"<html>Access denied</html>"
    with pytest.raises(ValueError, match="schedule table"):
        records(ctx, calendar)


def test_fed_calendar_waits_for_meeting_specific_policy_statement(ctx):
    src = source("fed_calendar", macro.FED)
    scheduled = records(ctx, src)
    assert [s.metadata["release_group"] for s in scheduled] == ["fomc", "minutes", "sep"]
    schedule = scheduled[0]
    assert schedule.metadata["time_precision"] == "estimated"
    assert schedule.scheduled_at.startswith("2026-11-04T19:00")
    ctx.due = [schedule.payload()]
    ctx.now += timedelta(days=3)
    ctx.job = "event_check"
    assert records(ctx, src) == []
    link = b'<a href="/newsevents/pressreleases/monetary20261104a.htm">HTML</a>'
    body = ctx.http.get(macro.FED).replace(b"</div>\n</div></html>", link + b"</div></div></html>")
    ctx.http.overrides[macro.FED] = body
    [published] = records(ctx, src)
    assert published.event_key == schedule.event_key and published.status == "published"
    assert published.url.endswith("monetary20261104a.htm")


def test_fred_series_overlap_and_decimal_spread(ctx):
    src = source("fred_market", macro.FRED)
    [market] = records(ctx, src)
    assert market.facts[-1].value == "25.00" and market.facts[-1].unit == "basis points"
    assert set(market.assets) == {"us2y", "us10y", "vix"}
    assert records(ctx, src) == []
    ctx.http.overrides[macro.FRED] = b"message\nquota exceeded"
    with pytest.raises(ValueError, match="columns"):
        records(ctx, src)


def test_bls_observation_bootstrap_without_calendar_and_missing_value(ctx):
    src = source("bls_actuals", macro.BLS_API)
    events = records(ctx, src)
    assert {s.metadata["release_group"] for s in events} == {"cpi", "ppi", "employment"}
    assert all(s.scheduled_at is None and s.published_at is None for s in events)
    assert all("reference month" in s.metadata["time_precision"] for s in events)
    assert records(ctx, src) == events
    body = b'{"status":"REQUEST_SUCCEEDED","Results":{"series":[{"seriesID":"s","data":[{"period":"M10","year":"2025","value":"-"}]}]}}'
    assert macro._observations(body) == {"s": {}}


def test_calendarless_prior_period_is_revised_when_next_period_arrives(ctx):
    src = source("bls_actuals", macro.BLS_API)
    prior = records(ctx, src)
    sep = next(e for e in prior if e.event_key == "bls:cpi:2026-09")
    assert sep.scheduled_at is None
    ctx.due = [sep.payload()]
    ctx.http.phase = 2
    new = records(ctx, src)
    updated = next(e for e in new if e.event_key == sep.event_key)
    assert updated.scheduled_at is None and updated.facts[3].value == "0.500"
    assert any(e.event_key == "bls:cpi:2026-10" for e in new)
    ctx.now += timedelta(days=100)
    expired = records(ctx, src)
    assert all(e.event_key != sep.event_key for e in expired)


def test_fred_authenticated_api_omits_key_from_evidence_and_cursor(ctx):
    src = source("fred_market", macro.FRED)
    ctx.env["FRED_API_KEY"] = "mock-test-secret"
    url = "https://api.stlouisfed.org/fred/series/observations"
    ctx.http.overrides[url] = b'{"observations":[{"date":"2026-10-30","value":"4.50"}]}'
    [market] = records(ctx, src)
    assert len(ctx.http.calls) == 3
    assert all("api_key=mock-test-secret" in query for query in ctx.http.calls)
    assert "mock-test-secret" not in repr([market.payload(), ctx.cursor])
