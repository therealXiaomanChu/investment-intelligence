from datetime import datetime, timezone

import pytest

from investment_intelligence.collectors import news, nvidia_newsroom
from investment_intelligence.configuration import Settings
from investment_intelligence.http import FixtureHttp
from investment_intelligence.pipeline import Pipeline
from investment_intelligence.storage import Database


def test_newsroom_job_enqueues_only_official_press_release_and_replays_once(tmp_path):
    settings = Settings({"OUTPUT_DIR": str(tmp_path)})
    db = Database("sqlite:///:memory:")
    try:
        pipeline = Pipeline(db, settings, FixtureHttp())
        instant = datetime(2026, 11, 2, 8, tzinfo=timezone.utc)
        assert pipeline.run("news_scan", instant, only="nvidia_newsroom")["changed_events"] == 1
        assert pipeline.run("news_scan", instant, only="nvidia_newsroom")["changed_events"] == 0
        [event] = db.all("SELECT * FROM events")
        assert event["importance"] >= 0.85
        assert event["published_at"] == event["event_at"] == "2026-11-01T20:00:00.000000+00:00"
        assert db.all("SELECT COUNT(*) AS n FROM alert_queue") == [{"n": 1}]
        assert db.all("SELECT source_id,canonical_url FROM documents") == [
            {
                "source_id": "nvidia_newsroom",
                "canonical_url": "https://nvidianews.nvidia.com/news/demo-board-statement",
            }
        ]
        assert "nvidia_news" in {s["id"] for s in settings.sources}
    finally:
        db.close()


def test_counsel_and_ceo_cooperation_require_substantive_affirmative_text():
    title = "Statement From the Independent Members of the NVIDIA Board"
    body = "The independent members of the NVIDIA Board have retained outside counsel to conduct an independent investigation."
    assert nvidia_newsroom.classify(title, body)[0] == "investigation_counsel_retained"
    assert nvidia_newsroom.classify(title, body.replace("have retained", "have not retained")) is None
    title = "A Message to Our Company"
    letter = (
        "NVIDIA founder and CEO Example Person sent the following email to employees. "
        "NVIDIA’s Board of Directors has initiated an independent investigation. "
        "I support the investigation and will cooperate fully."
    )
    assert nvidia_newsroom.classify(title, letter)[0] == "ceo_cooperation_statement"
    assert nvidia_newsroom.classify(title, "Rumors about the CEO's private life appeared.") is None


@pytest.mark.parametrize(
    "title,result",
    [
        ("NVIDIA Names Jane Doe as Its New Chief Financial Officer", "executive_appointment"),
        ("NVIDIA CEO Jane Doe Steps Down", "executive_departure"),
        ("NVIDIA CEO Jane Doe Denies Rumors of Retirement", None),
        ("NVIDIA Appoints a Partner Company CEO", None),
        ("NVIDIA and Partner Announce AI Product", None),
        ("Stock Falls Amid Reports of an Investigation", None),
    ],
)
def test_management_changes_distinguish_issuer_actions(title, result):
    action = nvidia_newsroom.classify(title, "")
    assert (action[0] if action else None) == result


def test_newsroom_rejects_cross_domain_items_and_decodes_publisher_cdata():
    raw = b"""<rss><channel><item><title>News</title>
    <link>https://nvidianews.nvidia.com/news/demo</link><pubDate>Sun, 08 Nov 2026 20:00:00 GMT</pubDate>
    <content>&lt;![CDATA[&lt;p&gt;An official finding.&lt;/p&gt;]]&gt;</content></item></channel></rss>"""
    [item] = list(news._rss(raw, newsroom=True))
    assert item.description == "An official finding."
    assert not list(
        news._rss(raw.replace(b"nvidianews.nvidia.com", b"nvidianews.nvidia.com.other.test"), newsroom=True)
    )
