"""arXiv OAI-PMH version dates, continuation and delivery to the regular research archive."""

from datetime import datetime, timezone

from investment_intelligence.collectors import research
from investment_intelligence.configuration import Context, Settings
from investment_intelligence.http import SourceError
from investment_intelligence.storage import Database


NOW = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)


def raw(number, version, latest):
    first = "Wed, 21 Oct 2026 04:20:10 GMT" if version == 2 else latest
    versions = f'<version version="v1"><date>{first}</date></version>'
    if version == 2:
        versions += f'<version version="v2"><date>{latest}</date></version>'
    return f"""<record><header><datestamp>2026-11-02</datestamp></header><metadata>
      <arXivRaw xmlns="http://arxiv.org/OAI/arXivRaw/">
      <id>2611.{number:05d}</id>{versions}<title>Speculative decoding</title>
      <authors>A Researcher and B Researcher</authors><abstract>A fixture describing inference.</abstract>
      <categories>cs.LG</categories></arXivRaw></metadata></record>"""


def response(body):
    return f'<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">{body}</OAI-PMH>'.encode()


def test_fallback_filters_by_actual_version_and_resumes_inside_page():
    calls = []

    class Http:
        def get(self, url, *, params):
            calls.append((url, params))
            if "export.arxiv.org" in url:
                raise SourceError("temporary timeout")
            if "openalex" in url:
                return b'{"results":[]}'
            assert url == research.OAI_URL
            if params.get("from") == "2026-11-02":
                entries = raw(1, 1, "Thu, 29 Oct 2026 08:00:00 GMT")
                entries += raw(2, 1, "Tue, 03 Nov 2026 11:03:12 GMT")
                entries += raw(3, 2, "Wed, 04 Nov 2026 14:01:00 GMT")
                return response("<ListRecords>" + entries + "</ListRecords>")
            return response('<error code="noRecordsMatch"/>')

    settings = Settings({"FETCH_LIMIT": "1"})
    source = next(s for s in settings.sources if s["id"] == "arxiv")
    cursor, papers = {}, []
    db = Database("sqlite:///:memory:")
    try:
        for _ in range(5):
            ctx = Context(settings, NOW, Http(), "paper_weekly", False, cursor, [], archive=db)
            papers.extend(research.collect(ctx, source))
            cursor = ctx.next_cursor
            if not cursor["incomplete"]:
                break
        assert not cursor["incomplete"]
        assert [r.external_id for r in papers] == ["2611.00002", "2611.00003"]
        assert papers[-1].event_at == "2026-11-04T14:01:00.000000+00:00"
        assert papers[-1].metadata["first_published_at"] == "2026-10-21T04:20:10+00:00"
        assert papers[-1].url.endswith("2611.00003v2")
        assert papers[-1].metadata["metadata_transport"].startswith("arXiv OAI-PMH")
        assert sum("export.arxiv.org" in url for url, _ in calls) == 1
    finally:
        db.close()


def test_expired_token_restarts_same_day_without_losing_the_week():
    requested = []

    class Http:
        def get(self, url, *, params):
            requested.append(params)
            if "resumptionToken" in params:
                return response('<error code="badResumptionToken"/>')
            return response("<ListRecords>" + raw(8, 1, "Wed, 04 Nov 2026 11:00:00 GMT") + "</ListRecords>")

    settings = Settings({"FETCH_LIMIT": "1"})
    ctx = Context(settings, NOW, Http(), "paper_weekly", False, {}, [])
    start, end = research.weekly_window(NOW)
    config = ctx.config("research")
    chosen, cursor, complete = research._harvest(
        ctx, config, start, end, {}, {"day": "2026-11-05", "token": "expired", "skip": 7}
    )
    assert set(chosen) == {"2611.00008"} and not complete
    assert requested[1]["from"] == "2026-11-05" and cursor["day"] == "2026-11-06"


def test_switch_from_existing_atom_archive_preserves_same_version_and_accepts_revision():
    class Archive:
        def all(self, query, k):
            return [{"metadata": '{"version":1}'}]

    class Http:
        def get(self, url, *, params):
            entries = raw(2, 1, "Tue, 03 Nov 2026 11:03:12 GMT")
            entries += raw(3, 2, "Wed, 04 Nov 2026 14:01:00 GMT")
            return response("<ListRecords>" + entries + "</ListRecords>")

    settings = Settings({"FETCH_LIMIT": "1"})
    ctx = Context(settings, NOW, Http(), "paper_weekly", False, {}, [], archive=Archive())
    seen = {"2611.00002": "old-atom-hash", "2611.00003": "another-atom-hash"}
    start, end = research.weekly_window(NOW)
    selected, _, _ = research._harvest(ctx, ctx.config("research"), start, end, seen, {})
    assert seen["2611.00002"] == "oai-v1"
    assert set(selected) == {"2611.00003"} and selected["2611.00003"][0]["version"] == 2


def test_crypto_mev_is_distinct_from_particle_energy_mev():
    cfg = {"topics": {"Crypto protocols": ["MEV", "maximal extractable value"]}}
    assert not research._matched({"title": "Particle mass", "abstract": "Energy 1 MeV"}, cfg)["Crypto protocols"]
    assert research._matched({"title": "Ethereum MEV", "abstract": ""}, cfg)["Crypto protocols"] == ["MEV"]


def test_openalex_secondary_batch_outage_preserves_verified_work():
    cfg = Settings({}).config("research")
    papers = [{"id": f"2611.{i:05d}", "doi": ""} for i in range(31)]
    ctx = Context(Settings({}), NOW, None, "paper_weekly", False, {}, [])

    class Http:
        def get(self, url, params):
            if "00030" in params["filter"]:
                raise SourceError("rate limited")
            return b'{"results":[{"id":"https://openalex.org/W100","doi":"10.48550/arxiv.2611.00000","authorships":[]}]}'

    ctx.http = Http()
    verified, pending = research._metadata(ctx, papers, cfg)
    assert verified["10.48550/arxiv.2611.00000"]["url"] == "https://openalex.org/W100"
    assert pending == {"10.48550/arxiv.2611.00030"} and ctx.warnings
