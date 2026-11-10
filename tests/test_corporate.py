from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import json

import pytest
import yaml

from investment_intelligence.collectors import corporate as corp
from investment_intelligence.fixtures import corporate_demo as demo
from investment_intelligence.storage import Database

ROOT = Path(__file__).resolve().parents[1]


class Transport:
    def __init__(self):
        self.calls = []
        self.phase = 0

    def get(self, url, **kwargs):
        self.calls.append(url)
        body = demo.response(url, self.phase)
        assert body is not None, url
        return body


@pytest.fixture()
def ctx():
    watchlist = yaml.safe_load((ROOT / "config/watchlist.example.yaml").read_text())
    watchlist["managers"] = [{"cik": demo.SA, "name": "Situational Awareness LP"}]
    errors = []
    return SimpleNamespace(
        http=Transport(),
        config=lambda name: yaml.safe_load((ROOT / f"config/{name}.yaml").read_text()),
        env={},
        demo=True,
        now=datetime(2026, 11, 2, 14, 10, tzinfo=timezone.utc),
        watchlist=watchlist,
        cursor={},
        next_cursor={},
        due=[],
        limit=30,
        backfill=False,
        warn=errors.append,
        errors=errors,
    )


def test_financial_revision_pipeline_sqlite(ctx, tmp_path):
    source = {"id": "sec_filings", "module": "corporate", "url": corp.BASE}
    db = Database("sqlite:///" + str(tmp_path / "filings.sqlite"))
    db.register(ctx.watchlist["assets"], [source])

    def ingest():
        records = list(corp.collect(ctx, source))
        with db.write() as conn:
            changed = sum(db.ingest(conn, r, ctx.now.isoformat()) for r in records)
        ctx.cursor = dict(ctx.next_cursor)
        return records, changed

    records, count = ingest()
    assert not ctx.errors and count == 1
    initial = records[0]
    facts = {f.metric: f for f in initial.facts}
    assert facts["revenue"].value == "100000"
    assert facts["free_cash_flow"].value == "40000"
    assert facts["revenue_growth_yoy"].value == "25.000"
    assert facts["prior_year_revenue"].period == "2025-07-01/2025-09-30"
    assert initial.assets == ("nvidia",)
    records, count = ingest()
    assert not records and not count
    ctx.http.phase = 1
    records, count = ingest()
    assert count == 2 and {r.kind for r in records} == {"10-Q", "8-K"}
    assert len(db.all("SELECT * FROM documents")) == 3
    assert len(db.all("SELECT * FROM events")) == 2
    historical = db.all("SELECT value FROM facts WHERE metric='revenue' ORDER BY vintage")
    assert [r["value"] for r in historical] == ["100000", "105000"]
    assert db.search("NVIDIA", 5)[0]["module"] == "corporate"
    ctx.backfill = True
    assert ingest()[1] == 0


def test_institutional_positions_are_separate_and_traceable(ctx):
    source = {"id": "sec_13f"}
    [r] = corp.collect(ctx, source)
    positions = {p["option"]: p for p in r.metadata["positions"]}
    assert positions["NON_OPTION"]["quantity_change"] == "20"
    assert positions["PUT"]["quantity_change"] == "-20"
    assert positions["PUT"]["weight_pct"] == "7.69231"
    assert len(r.metadata["comparison_evidence"]) == 2
    assert r.assets == ("nvidia",)
    ctx.cursor = dict(ctx.next_cursor)
    n = len(ctx.http.calls)
    assert list(corp.collect(ctx, source)) == []
    assert not any(path.endswith(".txt") for path in ctx.http.calls[n:])


def test_amendment_cannot_turn_additions_into_exits(ctx):
    base = demo._holdings(False)
    add = demo._holdings(True)
    amendment = (
        b"<DOCUMENT>\n<TYPE>13F-HR/A\n<XML><form><amendmentType>NEW HOLDINGS</amendmentType></form></XML></DOCUMENT>"
        + add
    )
    a = demo._filing("13F-HR", demo.PREVIOUS_13F, "2026-08-10", "2026-06-30", "primary_doc.xml")
    b = dict(a, form="13F-HR/A", filingDate="2026-08-15", accessionNumber=demo.LATEST_13F)
    ctx.http = SimpleNamespace(get=lambda url, **kw: amendment if demo.LATEST_13F in url else base)
    holdings, _ = corp._holdings_at(ctx, demo.SA, [a, b], {})
    assert holdings[("67066G104", "NON_OPTION", "SH")][0] == 220
    assert holdings[("67066G104", "PUT", "SH")][0] == 40
    missing = amendment.replace(b"NEW HOLDINGS", b"UNKNOWN")
    ctx.http = SimpleNamespace(get=lambda url, **kw: missing if demo.LATEST_13F in url else base)
    with pytest.raises(ValueError, match="Unsupported amendment"):
        corp._holdings_at(ctx, demo.SA, [a, b], {})


def test_earnings_date_and_release_match(ctx):
    [scheduled] = corp.collect(ctx, {"id": "earnings_calendar"})
    assert scheduled.status == "scheduled" and scheduled.metadata["date_precision"] == "day"
    assert scheduled.scheduled_at is None
    assert scheduled.metadata["check_at"][:16] == "2026-11-02T23:00"
    due = {"event_key": scheduled.event_key, "metadata": scheduled.metadata}
    ctx.due = [due]
    assert list(corp.collect(ctx, {"id": "earnings_results"})) == []
    ctx.http.phase = 1
    [published] = corp.collect(ctx, {"id": "earnings_results"})
    assert published.event_key == scheduled.event_key
    assert published.status == "published" and "2.02" in published.summary
    due["metadata"] = dict(scheduled.metadata, release_date="2026-11-01")
    assert list(corp.collect(ctx, {"id": "earnings_results"})) == []


def test_cik_identity_and_effective_period(ctx):
    spacex = next(a for a in ctx.watchlist["assets"] if a["symbol"] == "SPCX")
    assert corp._asset_ids([spacex], "2026-06-11") == ()
    assert corp._asset_ids([spacex], "2026-06-12") == ("spacex",)
    spacex["issuer_id"] = "0001924868"
    assert not any(spacex in group for group in corp._issuers(ctx).values())
    assert ctx.errors


def test_ticker_lookup_requires_matching_issuer_name(ctx):
    ctx.watchlist["assets"] = [dict(id="custom", symbol="NVDA", sec_name="NVIDIA CORP")]
    ctx.http = SimpleNamespace(
        get=lambda *args, **kwargs: json.dumps(
            {"0": {"ticker": "NVDA", "title": "NVIDIA CORP", "cik_str": 1045810}}
        ).encode()
    )
    assert list(corp._issuers(ctx, {"User-Agent": "test"})) == [demo.NV]
    ctx.watchlist["assets"][0]["sec_name"] = "other company"
    assert not corp._issuers(ctx, {"User-Agent": "test"})


def test_invalid_source_does_not_advance_and_is_retried(ctx):
    ctx.watchlist["assets"] = [a for a in ctx.watchlist["assets"] if a["symbol"] == "NVDA"]
    transport = ctx.http
    ctx.http = SimpleNamespace(get=lambda url, **kw: b'{"cik":1045810,"filings":{}}')
    assert list(corp.collect(ctx, {"id": "sec_filings"})) == []
    assert ctx.errors and not ctx.next_cursor
    ctx.http = transport
    assert list(corp.collect(ctx, {"id": "sec_filings"}))
    ctx.demo = False
    with pytest.raises(ValueError, match="SEC_USER_AGENT"):
        list(corp.collect(ctx, {"id": "sec_filings"}))


def test_prior_quarter_amendment_dates_comparison_to_latest_evidence(ctx, monkeypatch):
    base = demo._filing("13F-HR", demo.PREVIOUS_13F, "2026-08-10", "2026-06-30", "primary.xml")
    later = demo._filing("13F-HR", demo.LATEST_13F, "2026-10-29", "2026-09-30", "primary.xml")
    amended = demo._filing("13F-HR/A", demo.SA + "-26-990003", "2026-11-01", "2026-06-30", "primary.xml")
    monkeypatch.setattr(corp, "_submissions", lambda *a: ({}, [base, later, amended]))
    original = ctx.http.get

    def get(url, **kwargs):
        if url.endswith(amended["accessionNumber"] + ".txt"):
            return (
                b"<DOCUMENT>\n<TYPE>13F-HR/A\n<XML><form><amendmentType>RESTATEMENT</amendmentType></form>"
                b"</XML></DOCUMENT>" + demo._holdings(False)
            )
        return original(url, **kwargs)

    ctx.http.get = get
    [record] = corp.collect(ctx, {"id": "sec_13f"})
    assert record.published_at.startswith("2026-11-01T12:00")
    assert all(f.vintage.startswith("2026-11-01") for f in record.facts)
    assert record.metadata["report_period"] == "2026-09-30"
