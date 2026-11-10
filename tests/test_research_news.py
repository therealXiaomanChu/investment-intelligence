from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from investment_intelligence.collectors import news, research
from investment_intelligence.configuration import Context, Settings
from investment_intelligence.http import FixtureHttp, SourceError
from investment_intelligence.models import utc
from investment_intelligence.storage import Database

MONDAY = datetime(2026, 11, 2, 14, 10, tzinfo=timezone.utc)


@pytest.fixture
def system():
    config = Settings({}, demo=True)
    db = Database("sqlite:///:memory:")
    db.register(config.watchlist["assets"], config.sources)
    return SimpleNamespace(config=config, db=db, cursors={})


def run(system, source_id, *, phase=0, http=None, backfill=False):
    source = next(s for s in system.config.sources if s["id"] == source_id)
    module = research if source_id == "arxiv" else news
    ctx = Context(
        system.config,
        MONDAY,
        http or FixtureHttp(phase),
        "smoke",
        backfill,
        system.cursors.get(source_id, {}),
        [],
    )
    with system.db.write() as conn:
        records = list(module.collect(ctx, source))
        for record in records:
            system.db.ingest(conn, record, utc(MONDAY))
    system.cursors[source_id] = ctx.next_cursor
    return records, ctx.warnings


def test_weekly_window_with_clock_change():
    start, end = research.weekly_window(MONDAY)
    assert start.isoformat() == "2026-10-26T04:00:00+00:00"
    assert end.isoformat() == "2026-11-02T05:00:00+00:00"


def test_research_versions_disambiguation_and_reporting_period(system):
    first, warnings = run(system, "arxiv")
    assert len(first) == 6 and not warnings
    assert run(system, "arxiv")[0] == []
    updates, _ = run(system, "arxiv", phase=1)
    assert [p.external_id for p in updates] == ["9910.90001"]
    assert run(system, "arxiv", phase=1)[0] == []
    evidence = system.db.all("SELECT version FROM documents WHERE external_id='9910.90001' ORDER BY version")
    assert [r["version"] for r in evidence] == [1, 2]
    assert len(system.db.all("SELECT * FROM events")) == 6
    work = updates[0].metadata["openalex"]
    authors = work["authors"]
    assert len({a["id"] for a in authors}) == 2
    assert len({a["name"] for a in authors}) == 1
    assert len({a["institutions"][0]["id"] for a in authors}) == 2
    assert research._tracked(authors, {"tracked_author_ids": ["A99999102"]})
    assert not research._tracked(authors, {"tracked_author_ids": ["Alex Example"]})
    assert "speculative decoding" in updates[0].metadata["technical_tags"]
    assert "nvidia" in updates[0].assets
    assert system.db.search("quantization", 5)[0]["event_key"] == updates[0].event_key


def test_arxiv_openalex_outage_keeps_papers_and_retries(system):
    class Denied(FixtureHttp):
        def get(self, url, **kwargs):
            if "openalex.org" in url:
                raise SourceError("403 forbidden")
            return super().get(url, **kwargs)

    papers, warnings = run(system, "arxiv", http=Denied())
    assert len(papers) == 6 and warnings
    assert all(p.metadata["openalex"]["status"] == "unavailable" for p in papers)
    retry, _ = run(system, "arxiv")
    assert any(r.metadata["openalex"].get("url") for r in retry)
    assert len(system.db.all("SELECT * FROM events")) == 6


@pytest.mark.parametrize("backfill", [False, True])
def test_small_batch_resumes_without_dropping_papers(system, backfill):
    system.config.limit = 1
    collected = set()
    for _ in range(16):
        records, _ = run(system, "arxiv", backfill=backfill)
        assert len(records) <= 1
        collected.update(r.external_id for r in records)
        if not system.cursors["arxiv"]["incomplete"]:
            break
    assert len(collected) == (7 if backfill else 6)
    assert not system.cursors["arxiv"]["incomplete"]


def test_cross_source_announcement_keeps_both_evidence_and_asset_signals(system):
    records = [run(system, source)[0][0] for source in ["openai_news", "nvidia_news"]]
    assert records[0].event_key == records[1].event_key
    assert len(system.db.all("SELECT * FROM events")) == 1
    assert len(system.db.all("SELECT * FROM event_documents")) == 2
    assert {r["asset_id"] for r in system.db.all("SELECT * FROM signals")} == {"openai", "nvidia"}
    for source in ["openai_news", "nvidia_news"]:
        assert run(system, source)[0] == []


def test_upgrade_postponement_clears_announcement_and_preserves_history(system):
    [scheduled], _ = run(system, "ethereum_blog")
    assert scheduled.scheduled_at.startswith("2026-11-05T18:00")
    [postponed], _ = run(system, "ethereum_blog", phase=1)
    assert scheduled.event_key == postponed.event_key
    assert postponed.status == "postponed" and postponed.scheduled_at is None
    assert len(system.db.all("SELECT * FROM events")) == 1
    assert len(system.db.all("SELECT * FROM documents")) == 2
    assert run(system, "ethereum_blog", phase=1)[0] == []
    assert system.db.all("SELECT * FROM events")[0]["next_check"] is None


def test_same_fork_on_different_network_stays_separate(system):
    row = news.Item(
        "a",
        "https://a.example/fork",
        "DemoFork Hoodi Testnet Announcement",
        "Scheduled to activate on October 28, 2026 at 11:00 UTC.",
        "",
        "",
    )
    hoodi = news._upgrade(row)
    row.title = "DemoFork on Mainnet Postponed"
    mainnet = news._upgrade(row)
    assert hoodi[1].casefold() == "hoodi" and mainnet[1].casefold() == "mainnet"
    assert hoodi[2] == "scheduled" and mainnet[2] == "postponed"


def test_official_client_and_publisher_fallback(system):
    [client], _ = run(system, "agave_releases")
    assert (client.kind, client.assets) == ("client_release", ("solana",))
    assert "pre-release" in client.summary
    [btc], _ = run(system, "bitcoin_core")
    assert btc.assets == ("bitcoin",)
    [announcement], _ = run(system, "anthropic_news")
    assert announcement.published_at is None
    assert announcement.metadata["published_precision"] == "day"


def test_restricted_source_and_markup_drift_do_not_advance_cursor(system):
    class Restricted:
        def get(self, url):
            raise SourceError("HTTP 403")

    class Login:
        def get(self, url):
            return b"<html><h1>Subscription required</h1></html>"

    for http, error in [(Restricted(), SourceError), (Login(), ValueError)]:
        with pytest.raises(error):
            run(system, "nvidia_news", http=http)
        assert system.cursors == {}
    assert len(system.db.all("SELECT * FROM documents")) == 0
    [restored], _ = run(system, "nvidia_news")
    assert restored.source_id == "nvidia_news"


def test_capped_news_backfill_advances_on_repeated_invocations(system):
    system.config.limit = 1

    class History:
        def get(self, url):
            # Both entries are older than the normal seven-day startup window.
            head = "<rss><channel>"
            for i in (1, 2):
                head += (
                    f"<item><title>Historical notice {i}</title><guid>{i}</guid>"
                    f"<link>https://publisher.example/{i}</link>"
                    f"<pubDate>Thu, {i:02d} Oct 2026 08:00:00 GMT</pubDate></item>"
                )
            return (head + "</channel></rss>").encode()

    assert run(system, "nvidia_news", http=History())[0] == []
    ids = []
    for _ in range(3):
        records, _ = run(system, "nvidia_news", http=History(), backfill=True)
        ids.extend(r.external_id for r in records)
    assert ids == ["1", "2"]
    assert len(system.db.all("SELECT * FROM documents")) == 2


def test_openalex_merged_publisher_doi_requires_an_explicit_matching_location(system):
    import json

    class AlternateDOI(FixtureHttp):
        def get(self, url, **kwargs):
            raw = super().get(url, **kwargs)
            if "openalex.org" not in url:
                return raw
            body = json.loads(raw)
            work = body["results"][0]
            work["doi"] = "https://doi.org/10.5555/example-conference"
            work["locations"] = [{"landing_page_url": "http://arxiv.org/abs/9910.90001"}]
            return json.dumps(body).encode()

    papers, warnings = run(system, "arxiv", http=AlternateDOI())
    assert not warnings
    assert papers[0].metadata["openalex"]["authors"][0]["id"] == "https://openalex.org/A99999101"
    assert papers[1].metadata["openalex"] == {"status": "unavailable"}


def test_upgrade_discussion_does_not_confirm_an_absent_date():
    item = news.Item(
        "x",
        "https://publisher.example/x",
        "DemoFork Mainnet Announcement",
        "The upgrade is not yet scheduled. A meeting on August 1, 2027 will discuss it.",
        "",
        "",
    )
    assert news._upgrade(item) is None
    item.description = "The upgrade has been canceled."
    assert news._upgrade(item)[2:] == ("canceled", None, None)
