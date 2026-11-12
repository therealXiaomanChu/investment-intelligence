from dataclasses import replace
from datetime import datetime, timedelta, timezone

from investment_intelligence.collectors import macro
from investment_intelligence.configuration import Settings
from investment_intelligence.models import Fact, Record
from investment_intelligence.pipeline import Pipeline
from investment_intelligence.storage import Database


def test_calendar_discovers_published_actuals_once_and_preserves_revisions(tmp_path, monkeypatch):
    now = datetime(2026, 11, 12, 6, tzinfo=timezone.utc)
    release = (now - timedelta(hours=36)).isoformat()
    url = "https://www.bls.gov/news.release/archives/cpi_11102026.htm"
    key = "bls:cpi:2026-10"
    record = Record(
        source_id="bls_actuals",
        external_id=key,
        url=url,
        module="macro",
        kind="release",
        event_key=key,
        title="October CPI",
        summary="BLS published its October CPI.",
        event_at=release,
        published_at=release,
        importance=0.95,
        facts=(Fact("CPI", "337.476", "index", "2026-10"),),
        metadata={"publication_evidence": url},
    )
    incoming = record
    monkeypatch.setattr(macro, "collect", lambda ctx, source: [incoming])
    db = Database("sqlite:///:memory:")
    settings = Settings({"OUTPUT_DIR": str(tmp_path)})
    pipeline = Pipeline(db, settings)
    source = next(s for s in settings.sources if s["id"] == "bls_actuals")

    def scan(backfill=False):
        pipeline.collect(source, "calendar_sync", now, backfill, [])
        return len(db.all("SELECT alert_id FROM alert_queue"))

    incoming = replace(record, published_at=None, metadata={})
    assert scan() == 0
    incoming = record
    assert scan() == 1
    incoming = replace(record, summary="The publication page now links to a table.")
    assert scan() == 1
    incoming = replace(record, facts=(Fact("CPI", "337.500", "index", "2026-10"),))
    assert scan() == 2
    for offset in (-72, 12):
        stamp = (now + timedelta(hours=offset)).isoformat()
        incoming = replace(record, published_at=stamp, event_at=stamp)
        assert scan() == 2
    incoming = replace(record, facts=(Fact("CPI", "337.499", "index", "2026-10"),))
    assert scan(backfill=True) == 2
    db.close()
