from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import hashlib
import json
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup
import pytest

from investment_intelligence.collectors import news
from investment_intelligence.configuration import Context, Settings
from investment_intelligence.http import SourceError
from investment_intelligence.models import utc
from investment_intelligence.pipeline import Pipeline
from investment_intelligence.storage import Database, execute


NOW = datetime(2026, 11, 11, 18, tzinfo=timezone.utc)
SITE = "https://bitcoincore.org/en/2026/11/10/"


def feed(items):
    root = ET.Element("rss")
    channel = ET.SubElement(root, "channel")
    for slug, title, age, description in items:
        item = ET.SubElement(channel, "item")
        fields = {
            "title": title,
            "guid": slug,
            "link": SITE + slug + "/",
            "pubDate": format_datetime(NOW - timedelta(days=age)),
            "description": description,
        }
        for key, value in fields.items():
            ET.SubElement(item, key).text = value
    return ET.tostring(root)


def test_existing_cursor_rescores_only_advisories_and_preserves_alert_window(tmp_path):
    raw = feed(
        [
            (
                "disclose-recent",
                "CVE-2026-10001 - Notice",
                1,
                "<p>This issue is considered <strong>High</strong> severity.</p>",
            ),
            (
                "disclose-older",
                "CVE-2026-10002 - Notice",
                12,
                "<p>This issue is considered Critical severity.</p>",
            ),
            (
                "disclose-medium",
                "CVE-2026-10003 - Notice",
                1,
                "<p>This issue is considered Medium severity.</p>",
            ),
            ("ordinary-release", "An ordinary release", 1, "<p>This issue is considered High severity.</p>"),
        ]
    )

    class FeedOnly:
        def get(self, url):
            assert url == "https://bitcoincore.org/en/rss.xml"
            return raw

    settings = Settings({"OUTPUT_DIR": str(tmp_path / "reports")})
    source = next(s for s in settings.sources if s["id"] == "bitcoin_core")
    db = Database("sqlite:///:memory:")
    pipeline = Pipeline(db, settings, http=FeedOnly())
    ctx = Context(settings, NOW, FeedOnly(), "crypto_scan", False, {}, [])
    policy = settings.config("news")["publishers"]["bitcoin_core"]
    previous = {}
    with db.write() as conn:
        for item in news._rss(raw):
            record = news._record(ctx, source, policy, item, {})
            db.ingest(conn, record, item.published)
            previous[item.external_id] = {
                "stamp": hashlib.sha256(json.dumps(asdict(item), sort_keys=True).encode()).hexdigest(),
                "key": record.event_key,
            }
        execute(
            conn,
            "UPDATE sources SET cursor=:cursor WHERE source_id='bitcoin_core'",
            cursor=json.dumps({"items": previous, "last_scan": utc(NOW)}),
        )
    pipeline.run("crypto_scan", NOW, only="bitcoin_core")
    states = {r["event_key"]: r for r in db.all("SELECT event_key,kind,version,importance FROM events")}
    for slug, prior in previous.items():
        row = states[prior["key"]]
        expected = (
            (1, "protocol_news", 0.74)
            if slug == "ordinary-release"
            else (2, "protocol_news", 0.74 if slug == "disclose-medium" else 0.90)
        )
        assert (row["version"], row["kind"], row["importance"]) == expected
    assert db.all("SELECT event_key FROM alert_queue") == [{"event_key": previous["disclose-recent"]["key"]}]
    second = pipeline.run("crypto_scan", NOW + timedelta(minutes=15), only="bitcoin_core")
    assert second["changed_events"] == 0
    db.close()


def test_truncated_advisory_retries_official_page_after_transient_failure():
    slug, title = "disclose-retry", "CVE-2026-10004 - Notice"
    raw = feed([(slug, title, 1, "<p>Earlier reports mentioned high severity.</p>")])
    settings = Settings({})
    source = next(s for s in settings.sources if s["id"] == "bitcoin_core")

    class FeedAndPage:
        failures = 1

        def get(self, url):
            if url == source["url"]:
                return raw
            assert url == SITE + slug + "/"
            if self.failures:
                self.failures -= 1
                raise SourceError("temporary failure")
            return f'<article class="h-entry"><h1>{title}</h1><div class="e-content"><p>This issue is considered Critical severity.</p></div></article>'.encode()

    http = FeedAndPage()
    first = Context(settings, NOW, http, "crypto_scan", False, {}, [])
    assert list(news.collect(first, source)) == [] and first.warnings
    later = Context(settings, NOW + timedelta(days=10), http, "crypto_scan", False, first.next_cursor, [])
    [record] = list(news.collect(later, source))
    assert record.importance >= 0.85 and record.event_at == utc(NOW - timedelta(days=1))
    assert any(f.metric == "publisher_severity" and f.value == "critical" for f in record.facts)
    again = Context(settings, NOW + timedelta(days=11), http, "crypto_scan", False, later.next_cursor, [])
    assert list(news.collect(again, source)) == []


@pytest.mark.parametrize(
    "fragment, expected",
    [
        ("<p>This issue is considered Low severity and easily discoverable.</p>", "low"),
        ("<p>There is high severity in some unrelated cases.</p>", None),
        ("<blockquote><p>This issue is considered High severity.</p></blockquote>", None),
        ("<p>This issue is considered High severity.</p><p>This issue is considered Low severity.</p>", None),
    ],
)
def test_only_explicit_unambiguous_publisher_ratings(fragment, expected):
    assert news._bitcoin_severity(BeautifulSoup(fragment, "html.parser")) == expected
