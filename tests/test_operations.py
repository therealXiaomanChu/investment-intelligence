from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
import requests

from investment_intelligence import delivery
from investment_intelligence.configuration import Settings
from investment_intelligence.http import FixtureHttp, Http, SourceError
from investment_intelligence.models import utc
from investment_intelligence.pipeline import Pipeline, health_text
from investment_intelligence.schedule import eligible, week_window
from investment_intelligence.storage import Database

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 2, 14, 10, tzinfo=timezone.utc)
FEED = "https://official.invalid/public-feed"


class Response:
    def __init__(self, status, body=b"ok", headers=None):
        self.status_code = status
        self.body = body
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, size):
        yield self.body


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def test_bounded_http_retries_honors_retry_after_and_read_timeout():
    waits = []
    session = Session([Response(429, headers={"Retry-After": "4"}), Response(503), Response(200)])
    http = Http({}, session=session, pause=waits.append)
    assert http.get(FEED) == b"ok"
    assert len(session.calls) == 3 and 4 in waits and 2 in waits
    assert all(c[2]["timeout"][0] > 0 and c[2]["timeout"][1] > 0 for c in session.calls)
    session = Session([requests.ReadTimeout("secret query"), requests.ConnectionError(), Response(200)])
    assert Http({}, session=session, pause=lambda _: None).get(FEED) == b"ok"


def test_http_stops_retrying_redacts_secrets_and_bounds_body(monkeypatch):
    session = Session([Response(429, headers={"Retry-After": "300"})])
    with pytest.raises(SourceError, match="retry on next run"):
        Http({}, session=session, pause=lambda _: None).get(FEED)
    assert len(session.calls) == 1
    session = Session([requests.ReadTimeout("mysecret")] * 3)
    with pytest.raises(SourceError, match="ReadTimeout") as err:
        Http({}, session=session, pause=lambda _: None).get(FEED, params={"api_key": "mysecret"})
    assert "mysecret" not in str(err.value) and len(session.calls) == 3
    session = Session([Response(403)])
    with pytest.raises(SourceError, match="403"):
        Http({}, session=session, pause=lambda _: None).get(FEED)
    assert len(session.calls) == 1
    monkeypatch.setattr("investment_intelligence.http.MAX_RESPONSE_BYTES", 2)
    with pytest.raises(SourceError, match="exceeds"):
        Http({}, session=Session([Response(200, b"123")]), pause=lambda _: None).get(FEED)


def test_no_sec_request_without_identifying_agent():
    session = Session([])
    with pytest.raises(SourceError, match="SEC_USER_AGENT"):
        Http({}, session=session).get("https://data.sec.gov/submissions/CIK0001045810.json")
    assert not session.calls


@pytest.mark.parametrize(
    ("day", "expected_hour"), [("2026-10-30", 13), ("2026-11-02", 14), ("2027-03-12", 14), ("2027-03-15", 13)]
)
def test_premarket_respects_both_dst_changes(day, expected_hour):
    start = datetime.fromisoformat(day).replace(hour=9, minute=10, tzinfo=NY)
    assert "premarket_brief" not in eligible(start - timedelta(minutes=1), NY)
    assert datetime.fromisoformat(eligible(start, NY)["premarket_brief"]).hour == expected_hour
    delayed = eligible(start + timedelta(minutes=37), NY)
    assert delayed["premarket_brief"] == utc(start)
    assert "after_close" not in delayed


def test_us_market_holidays_and_weekend_crypto_across_time_zones():
    thanksgiving = datetime(2026, 11, 26, 18, tzinfo=NY)
    assert "premarket_brief" not in eligible(thanksgiving, NY)
    assert "after_close" not in eligible(thanksgiving, NY)
    saturday = datetime(2026, 11, 7, 13, tzinfo=NY)
    slots = eligible(saturday, NY)
    assert slots["crypto_scan"] == utc(saturday.replace(hour=12))
    assert "sec_poll" not in slots and "premarket_brief" not in slots
    china = ZoneInfo("Asia/Shanghai")
    # 21:50 Monday ET is Tuesday morning in Shanghai.
    evening = datetime(2026, 11, 2, 21, 50, tzinfo=NY)
    assert eligible(evening, china)["sec_poll"] == utc(evening.replace(minute=45))
    assert "sec_poll" not in eligible(evening + timedelta(hours=2), china)
    assert eligible(evening, china)["after_close"] == utc(evening.replace(hour=16, minute=40))


def test_weekly_windows_use_local_midnight_and_calendar_days():
    start, end = week_window(NOW, NY)
    assert start == "2026-10-26T04:00:00.000000+00:00"
    assert end == "2026-11-02T05:00:00.000000+00:00"
    assert datetime.fromisoformat(end) - datetime.fromisoformat(start) == timedelta(hours=169)


def _outbox(tmp_path, env=None):
    db = Database("sqlite:///" + str(tmp_path / "outbox.sqlite"))
    opts = {"OUTPUT_DIR": str(tmp_path / "briefs"), **(env or {})}
    settings = Settings(env=opts)
    rid = delivery.snapshot(db, settings, "daily-2030-01-01", "daily.md", [], "Daily", NOW)
    return db, settings, rid


def smtp_env():
    return {
        "SMTP_HOST": "mail.invalid",
        "SMTP_FROM": "sender@example.invalid",
        "SMTP_TO": "recipient@example.invalid",
    }


def test_markdown_and_smtp_are_idempotent_and_message_id_stable(tmp_path):
    db, settings, rid = _outbox(tmp_path, smtp_env())
    sent = []
    filename, status = delivery.deliver(db, settings, rid, lambda env, m: sent.append(m))
    assert status == "sent"
    from pathlib import Path

    target = Path(filename)
    body = target.read_text()
    target.unlink()
    delivery.snapshot(db, settings, rid, "daily.md", [], "Changed title", NOW + timedelta(hours=1))
    assert delivery.deliver(db, settings, rid, lambda env, m: sent.append(m))[1] == "sent"
    assert body == target.read_text() and "Changed title" not in body
    archived = db.all("SELECT * FROM reports")[0]
    assert (
        len(sent) == 1
        and sent[0]["Message-ID"] == delivery.mail_message(settings.env, archived)["Message-ID"]
    )


def test_no_credentials_and_uncertain_delivery_states(tmp_path):
    db, settings, rid = _outbox(tmp_path)
    assert delivery.deliver(db, settings, rid, lambda *_: pytest.fail("unexpected SMTP"))[1] == "disabled"
    folder = tmp_path / "uncertain"
    env = smtp_env()
    db, settings, rid = _outbox(folder, env)

    def ambiguous(env, msg):
        raise ConnectionResetError("connection closed after DATA")

    assert delivery.deliver(db, settings, rid, ambiguous)[1] == "uncertain"
    assert delivery.deliver(db, settings, rid, lambda *_: pytest.fail("duplicate SMTP"))[1] == "uncertain"
    assert db.all("SELECT mail_error FROM reports") == [{"mail_error": "ConnectionResetError"}]


def test_invalid_smtp_config_can_be_repaired_without_losing_report(tmp_path):
    db, settings, rid = _outbox(tmp_path, {"SMTP_HOST": "mail.invalid"})
    assert (
        delivery.deliver(db, settings, rid, lambda *_: pytest.fail("unexpected SMTP"))[1]
        == "configuration_error"
    )
    assert db.all("SELECT mail_state FROM reports") == [{"mail_state": "ready"}]
    settings.env.update(smtp_env())
    assert delivery.deliver(db, settings, rid, lambda *_: None)[1] == "sent"


def test_source_recovery_commits_only_successful_cursor_and_doctor_reports_stale(tmp_path):
    settings = Settings(env={"OUTPUT_DIR": str(tmp_path / "output")})
    settings.sources = [s for s in settings.sources if s["id"] == "bea_calendar"]
    db = Database("sqlite:///" + str(tmp_path / "health.sqlite"))
    pipeline = Pipeline(db, settings, http=FixtureHttp())
    assert pipeline.run("calendar_sync", NOW)["changed_events"] > 0
    before = db.all("SELECT cursor,last_success FROM sources")[0]

    class Interrupted:
        def get(self, url):
            raise SourceError("connection unavailable")

    pipeline.http = Interrupted()
    later = NOW + timedelta(days=1)
    failure = pipeline.run("calendar_sync", later)
    assert failure["coverage"] == "partial"
    assert db.all("SELECT cursor,last_success FROM sources")[0] == before
    assert "unavailable" in health_text(db, settings, later)
    pipeline.http = FixtureHttp()
    result = pipeline.run("source_health", later)
    assert result["coverage"] == "ok" and result["reports"][0]["email"] == "disabled"
    assert db.all("SELECT health,last_success FROM sources") == [{"health": "ok", "last_success": utc(later)}]
    assert "stale" in health_text(db, settings, later + timedelta(days=5))
