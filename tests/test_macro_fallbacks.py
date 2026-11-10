from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from investment_intelligence.collectors import macro
from investment_intelligence.http import SourceError
from test_macro import ctx, records, source
from test_system import system


def program(title, release, clock="08:30 AM"):
    return f"""<h1>Schedule of Releases for the {title}</h1><table>
        <tr><th>Reference Month</th><th>Release Date</th><th>Release Time</th></tr>
        <tr><td>October 2026</td><td>{release}</td><td>{clock}</td></tr></table>""".encode()


def test_bls_recovers_with_program_calendars_without_guessing_dates(ctx):
    def get(url):
        if url.endswith(".ics"):
            raise SourceError("www.bls.gov HTTP 403")
        if url.endswith("cpi.htm"):
            return program("Consumer Price Index", "Nov. 10, 2026")
        if url.endswith("ppi.htm"):
            return program("Producer Price Index", "Nov. 13, 2026", "TBD")
        raise SourceError("www.bls.gov HTTP 503")

    ctx.http.get = get
    [cpi] = records(ctx, source("bls_calendar", macro.BLS))
    assert cpi.event_key == "bls:cpi:2026-10"
    assert cpi.scheduled_at == "2026-11-10T13:30:00.000000+00:00"
    assert cpi.url == "https://www.bls.gov/schedule/news_release/cpi.htm"
    assert cpi.metadata["check_source"] == "bls_actuals"
    assert any("no usable official date/time" in warning for warning in ctx.warnings)
    assert any("employment" in warning for warning in ctx.warnings)


def test_bls_program_summer_dst_and_title_guard(ctx):
    ctx.now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    page = program("Consumer Price Index", "Sept. 10, 2026")
    ctx.http.get = lambda url: page.replace(b"October 2026", b"August 2026")
    src = source("bls_calendar", macro.BLS)
    [cpi] = list(macro._bls_program(ctx, src, "cpi", "Consumer Price Index", macro.BLS))
    assert cpi.scheduled_at == "2026-09-10T12:30:00.000000+00:00"
    with pytest.raises(ValueError, match="title"):
        list(macro._bls_program(ctx, src, "ppi", "Producer Price Index", macro.BLS))


H15 = b"""<table id="h15table">
<tr><th class="colhead" id="col1">2026<br>Oct<br>30</th></tr>
<tr><th id="c">Treasury constant maturities</th></tr>
<tr><th id="n" headers="c">Nominal <a>9</a></th></tr>
<tr><th headers="c n">2-year</th><td headers="n col1">3.75</td></tr>
<tr><th headers="c n">10-year</th><td headers="n col1">4.00</td></tr>
<tr><th id="r">Inflation indexed</th></tr>
<tr><th headers="c r">10-year</th><td headers="r col1">1.07</td></tr></table>"""
VIX = b"DATE,OPEN,HIGH,LOW,CLOSE\n10/30/2026,19.0,23.0,18.0,21.200000\n"


def test_direct_publisher_fallback_keeps_spread_and_source_urls(ctx):
    def get(url, **kwargs):
        if url == macro.FRED:
            raise SourceError("timeout")
        return {macro.H15: H15, macro.VIX_HISTORY: VIX}[url]

    ctx.http.get = Mock(side_effect=get)
    src = source("fred_market", macro.FRED)
    [market] = records(ctx, src)
    assert ctx.http.get.call_count == 3
    assert [fact.value for fact in market.facts] == ["4.00", "3.75", "21.200000", "25.00"]
    assert market.facts[-1].unit == "basis points"
    assert market.metadata["series_urls"] == {
        "DGS2": macro.H15, "DGS10": macro.H15, "VIXCLS": macro.VIX_HISTORY,
    }
    ctx.cursor["snapshots"]["2026-10-30"]["VIXCLS"] = "21.20"
    assert records(ctx, src) == []  # Precision formatting alone does not change the event.


def test_one_original_source_can_fail_without_erasing_an_archived_fact(ctx):
    def get(url, **kwargs):
        if url == macro.VIX_HISTORY:
            return VIX
        raise SourceError("host unavailable")

    src = source("fred_market", macro.FRED)
    ctx.http.get = Mock(side_effect=get)
    [vix] = records(ctx, src)
    assert vix.url == macro.VIX_HISTORY and len(vix.facts) == 1
    assert any(macro.H15 in note for note in ctx.warnings)
    ctx.cursor["snapshots"]["2026-10-30"].update(DGS2="3.75", DGS10="4.00")
    assert records(ctx, src) == []
    assert "DGS10" in ctx.cursor["snapshots"]["2026-10-30"]


def test_unavailable_calendar_probe_has_bounded_persistent_slots(tmp_path):
    pipeline = system(tmp_path)
    now = datetime(2026, 11, 2, 13, 35, tzinfo=timezone.utc)
    assert not pipeline.claim_bls_probe(now)
    with pipeline.db.write() as conn:
        conn.exec_driver_sql("UPDATE sources SET health='unavailable' WHERE source_id='bls_calendar'")
    for offset in range(6):
        slot = now + timedelta(minutes=15 * offset)
        assert pipeline.claim_bls_probe(slot)
        assert not pipeline.claim_bls_probe(slot + timedelta(minutes=1))
    assert not pipeline.claim_bls_probe(now - timedelta(minutes=1))
    assert not pipeline.claim_bls_probe(now + timedelta(minutes=90))
    assert not pipeline.claim_bls_probe(now - timedelta(days=1))
    pipeline.db.close()


def test_probe_alerts_only_on_a_newly_seen_current_reference_period(tmp_path):
    pipeline = system(tmp_path)
    now = datetime(2026, 11, 2, 13, 35, tzinfo=timezone.utc)
    def probe(at):
        return pipeline.run("event_check", at, only="bls_actuals", discover_bls=True)
    assert not probe(now)["reports"]  # first-run baseline
    assert not probe(now + timedelta(minutes=15))["reports"]  # unchanged old reference month
    pipeline.http.phase = 1
    assert probe(now + timedelta(minutes=30))["reports"]
    [event] = pipeline.db.all("SELECT * FROM events WHERE event_key='bls:cpi:2026-10'")
    assert event["published_at"] is None and event["scheduled_at"] is None
    assert event["event_at"].startswith("2026-11-02T14:05:00")
    assert "first observed" in event["metadata"]
    assert not probe(now + timedelta(minutes=45))["reports"]
    pipeline.db.close()


def test_probe_never_promotes_an_old_baseline_to_a_fresh_alert(ctx):
    ctx.job = "bls_probe"
    src = source("bls_actuals", macro.BLS_API)
    assert not records(ctx, src)
    ctx.http.phase = 1
    ctx.now += timedelta(days=2)
    assert not records(ctx, src)


def test_probe_preserves_first_observation_from_earlier_daily_check(ctx):
    ctx.job = "bls_probe"
    src = source("bls_actuals", macro.BLS_API)
    assert not records(ctx, src)
    ctx.now += timedelta(hours=20)
    ctx.http.phase = 1
    ctx.job = "calendar_sync"
    records(ctx, src)
    morning = ctx.now
    ctx.now += timedelta(hours=1)
    ctx.job = "bls_probe"
    advanced = records(ctx, src)
    cpi = next(r for r in advanced if r.event_key == "bls:cpi:2026-10")
    assert datetime.fromisoformat(cpi.event_at) == morning
    assert cpi.published_at is None
