from datetime import datetime, timezone

import pytest

from investment_intelligence.configuration import Settings
from investment_intelligence import cli, delivery
from investment_intelligence.storage import Database


def test_gmail_credentials_do_not_cross_into_custom_smtp():
    gmail = {"GMAIL_ADDRESS": "me@example.org", "GMAIL_APP_PASSWORD": "test only"}
    env = delivery.mail_environment({**gmail, "SMTP_TO": "alerts@example.org"})
    assert (env["SMTP_HOST"], env["SMTP_FROM"], env["SMTP_TO"]) == (
        "smtp.gmail.com",
        "me@example.org",
        "alerts@example.org",
    )
    assert env["SMTP_PASSWORD"] == "testonly"
    custom = delivery.mail_environment({**gmail, "SMTP_HOST": "relay.example.org"})
    assert "SMTP_PASSWORD" not in custom and "SMTP_USER" not in custom


def test_cloud_alerts_wait_for_credentials_and_skip_briefs(tmp_path):
    env = {"EMAIL_SCOPE": "alerts", "GMAIL_ADDRESS": "me@example.org", "OUTPUT_DIR": str(tmp_path)}
    settings = Settings(env)
    db = Database("sqlite:///:memory:")
    now = datetime.now(timezone.utc)
    try:
        for rid in ("premarket_brief-today", "alert-1"):
            delivery.snapshot(db, settings, rid, rid + ".md", [], "Example", now)
        sent = []

        def record(env, msg):
            sent.append(msg["To"])

        assert delivery.deliver(db, settings, "premarket_brief-today", record)[1] == "disabled"
        assert delivery.deliver(db, settings, "alert-1", record)[1] == "configuration_error"
        fixed = Settings({**env, "GMAIL_APP_PASSWORD": "local test value"})
        assert delivery.deliver(db, fixed, "alert-1", record)[1] == "sent"
        assert delivery.deliver(db, fixed, "alert-1", record)[1] == "sent"
        assert sent == ["me@example.org"]
    finally:
        db.close()


def test_test_email_works_on_clean_runner_without_state(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GMAIL_ADDRESS", "inbox@example.org")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "test value")
    monkeypatch.setenv("EMAIL_SCOPE", "alerts")
    monkeypatch.delenv("SMTP_HOST", raising=False)
    sent = []
    monkeypatch.setattr(delivery, "smtp", lambda env, msg: sent.append(msg))
    assert cli.main(["test-email"]) == 0
    assert len(sent) == 1 and sent[0]["To"] == "inbox@example.org"
    assert "email test" in sent[0]["Subject"]
    assert not list(tmp_path.iterdir())


def test_missing_email_configuration_has_clear_error():
    with pytest.raises(ValueError, match="GMAIL_ADDRESS"):
        delivery.send_test_email({})
