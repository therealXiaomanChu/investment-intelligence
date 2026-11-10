"""Full-pipeline acceptance. The PostgreSQL parity test uses a unique disposable schema."""

from datetime import datetime, timedelta, timezone
import json
import os
import socket
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from investment_intelligence.cli import main
from investment_intelligence.configuration import Settings
from investment_intelligence.demo import run_demo
from investment_intelligence.http import FixtureHttp, SourceError
from investment_intelligence.pipeline import Pipeline
from investment_intelligence.storage import Database, execute

NOW = datetime(2026, 11, 2, 14, 10, tzinfo=timezone.utc)


def system(tmp_path, url="sqlite:///:memory:"):
    settings = Settings({"OUTPUT_DIR": str(tmp_path)}, demo=True)
    settings.watchlist["managers"] = [{"name": "Situational Awareness", "cik": "0002045724"}]
    db = Database(url)
    return Pipeline(db, settings, FixtureHttp())


def test_airgapped_cli_and_demo_replay(tmp_path, monkeypatch, capsys):
    def blocked(*args, **kwargs):
        pytest.fail("The offline demo attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    for name in ("SMTP_HOST", "LLM_API_KEY", "SEC_USER_AGENT", "DATABASE_URL"):
        monkeypatch.setenv(name, "must-never-be-used")
    assert main(["demo", "--output", str(tmp_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert set(result["modules"]) == {"corporate", "research", "macro", "news"} and not result["warnings"]
    original = {p.name: p.read_bytes() for p in (tmp_path / "demo").glob("*.md")}
    for name in result["modules"]:
        body = original[f"{name}.md"]
        assert b"[Source](" in body and b"synthetic" in body
    assert original["research.md"].count(b"\n### ") == 5
    again = run_demo(tmp_path)
    assert again["document_versions"] == result["document_versions"]
    assert original == {p.name: p.read_bytes() for p in (tmp_path / "demo").glob("*.md")}
    db = Database("sqlite:///" + result["database"])
    try:
        assert db.search("CPI", 1)
        assert db.all("SELECT DISTINCT mail_state FROM reports") == [{"mail_state": "disabled"}]
        assert not db.all("SELECT * FROM spend")
    finally:
        db.close()


def test_rollback_only_dry_run_preserves_state_and_money(tmp_path):
    url = "sqlite:///" + str(tmp_path / "stored.sqlite")
    real = system(tmp_path, url)
    real.run("news_scan", NOW)
    counts = real.db.all("SELECT COUNT(*) AS n FROM documents")
    real.db.close()
    preview = Database(url, preview=True)
    settings = Settings({"OUTPUT_DIR": str(tmp_path / "preview")}, demo=True)
    run = Pipeline(preview, settings, FixtureHttp())
    result = run.run("crypto_scan", NOW)
    assert result["changed_events"] > 0 and result["preview"] and not result["reports"]
    preview.close()
    db = Database(url)
    assert db.all("SELECT COUNT(*) AS n FROM documents") == counts
    assert not db.all("SELECT * FROM jobs WHERE job_name='crypto_scan'")
    assert not (tmp_path / "preview").exists()
    db.close()


def test_release_retries_stop_and_recover(tmp_path):
    p = system(tmp_path)
    p.run("calendar_sync", NOW)
    due = p.due(NOW)
    assert any(e["metadata"].get("check_source") == "bls_actuals" for e in due)
    for offset in (0, 6, 17, 33, 49, 65):
        p.run("event_check", NOW + timedelta(minutes=offset), only="bls_actuals")
    assert not [
        e for e in p.due(NOW + timedelta(hours=2)) if e["metadata"].get("check_source") == "bls_actuals"
    ]
    assert p.db.all("SELECT event_key FROM events WHERE attempts=6 AND status='delayed'")
    p.http.phase = 1
    p.run("source_health", NOW + timedelta(hours=2), only="bls_actuals")
    assert not p.db.all("SELECT event_key FROM events WHERE attempts=6 AND status='delayed'")
    p.db.close()


def test_recovery_preserves_failed_cursor_and_catches_up(tmp_path):
    p = system(tmp_path)
    src = "ethereum_blog"
    p.run("crypto_scan", NOW, only=src)
    before = p.db.all("SELECT cursor FROM sources WHERE source_id=:s", s=src)

    class Failure:
        def get(self, *a, **kw):
            raise SourceError("fixture host HTTP 503")

    p.http = Failure()
    result = p.run("crypto_scan", NOW + timedelta(hours=4), only=src)
    assert result["coverage"] == "partial"
    assert p.db.all("SELECT cursor FROM sources WHERE source_id=:s", s=src) == before
    p.http = FixtureHttp(1)
    p.run("source_health", NOW + timedelta(hours=5), only=src)
    assert p.db.all("SELECT health FROM sources WHERE source_id=:s", s=src) == [{"health": "ok"}]
    postponed = p.db.all("SELECT * FROM events WHERE status='postponed'")
    assert postponed and postponed[0]["scheduled_at"] is None
    p.db.close()


def test_dispatch_idempotent_and_weekly_continuation(tmp_path):
    p = system(tmp_path)
    runs = p.dispatch(NOW)
    assert {"paper_weekly", "premarket_brief", "crypto_scan"} <= {r["job"] for r in runs}
    assert not p.dispatch(NOW)
    p.db.close()
    limited = system(tmp_path / "limited")
    limited.settings.limit = 2
    for i in range(18):
        limited.dispatch(NOW + timedelta(minutes=10 * i))
        if limited.db.all("SELECT slot FROM slots WHERE job_name='paper_weekly'"):
            break
    assert limited.db.all("SELECT slot FROM slots WHERE job_name='paper_weekly'")
    weekly = limited.db.all("SELECT body FROM reports WHERE report_id='paper_weekly-2026-10-26'")
    assert weekly and weekly[0]["body"].count("\n### ") == 5
    limited.db.close()


def observations(p):
    for phase, jobs in enumerate(
        (
            ("calendar_sync", "sec_poll", "paper_weekly", "crypto_scan", "news_scan"),
            ("event_check", "sec_poll", "crypto_scan", "paper_weekly"),
            ("calendar_sync", "premarket_brief", "after_close"),
        )
    ):
        p.http.phase = phase
        for job in jobs:
            r = p.run(job, NOW + timedelta(minutes=phase))
            assert not r["warnings"], r
    snap = {}
    for table, order in (
        ("documents", "document_id"),
        ("events", "event_key"),
        ("facts", "document_id,ordinal"),
        ("event_documents", "event_key,document_id"),
        ("signals", "event_key,asset_id"),
        ("reports", "report_id"),
        ("jobs", "job_name"),
        ("sources", "source_id"),
    ):
        snap[table] = p.db.all(f"SELECT * FROM {table} ORDER BY {order}")
    assert {e["module"] for e in snap["events"]} == {"news", "macro", "research", "corporate"}
    assert p.db.search("NVIDIA", 3)
    return snap


@pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"), reason="set TEST_DATABASE_URL to a disposable PostgreSQL database"
)
def test_full_postgres_matches_sqlite(tmp_path):
    url = make_url(os.environ["TEST_DATABASE_URL"]).set(drivername="postgresql+psycopg")
    assert url.get_backend_name() == "postgresql"
    engine = create_engine(url)
    schema = "iitest_" + uuid.uuid4().hex
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE SCHEMA " + schema)
    try:
        isolated = url.update_query_dict({"options": "-csearch_path=" + schema}).render_as_string(
            hide_password=False
        )
        pg = system(tmp_path / "pg", isolated)
        local = system(tmp_path / "sqlite")
        try:
            assert observations(pg) == observations(local)
            with pg.db.run_lock():
                with pytest.raises(RuntimeError, match="Another pipeline"):
                    with pg.db.run_lock():
                        pass
            before = pg.db.all("SELECT COUNT(*) AS n FROM documents")
            preview = Database(isolated, preview=True)
            with preview.write() as conn:
                execute(conn, "UPDATE assets SET symbol='preview' WHERE asset_id='nvidia'")
            assert preview.all("SELECT symbol FROM assets WHERE asset_id='nvidia'")[0]["symbol"] == "preview"
            preview.close()
            assert pg.db.all("SELECT symbol FROM assets WHERE asset_id='nvidia'")[0]["symbol"] == "NVDA"
            assert pg.db.all("SELECT COUNT(*) AS n FROM documents") == before
        finally:
            pg.db.close()
            local.db.close()
    finally:
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP SCHEMA " + schema + " CASCADE")
        engine.dispose()


def test_crash_after_collection_still_delivers_alert_and_markdown(tmp_path, monkeypatch):
    p = system(tmp_path)
    original = p.collect

    def interrupt(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("simulated process interruption")

    p.http.phase = 1
    monkeypatch.setattr(p, "collect", interrupt)
    with pytest.raises(RuntimeError, match="interruption"):
        p.run("sec_poll", NOW, only="sec_filings")
    pending = p.db.all("SELECT alert_id FROM alert_queue WHERE report_id IS NULL")
    assert pending
    monkeypatch.setattr(p, "collect", original)
    import investment_intelligence.pipeline as orchestration

    send = orchestration.deliver

    def disk_failure(*args):
        raise OSError("simulated disk outage")

    monkeypatch.setattr(orchestration, "deliver", disk_failure)
    with pytest.raises(OSError):
        p.run("sec_poll", NOW, only="sec_filings")
    monkeypatch.setattr(orchestration, "deliver", send)
    recovered = p.run("sec_poll", NOW, only="sec_filings")
    assert recovered["changed_events"] == 0
    assert all((tmp_path / (r["alert_id"] + ".md")).exists() for r in pending)
    assert len(p.db.all("SELECT report_id FROM reports")) == len(pending)
    assert p.run("sec_poll", NOW, only="sec_filings")["reports"] == []
    p.db.close()


def test_empty_upstream_failure_does_not_seal_week(tmp_path):
    p = system(tmp_path)
    real = p.http

    class Failed:
        def get(self, *args, **kwargs):
            raise SourceError("arXiv HTTP 503")

    p.http = Failed()
    failed = p.run("paper_weekly", NOW)
    assert failed["continuation"] and failed["coverage"] == "partial"
    assert not p.db.all("SELECT 1 FROM reports WHERE report_id='paper_weekly-2026-10-26'")
    p.http = real
    recovered = p.run("paper_weekly", NOW + timedelta(hours=2))
    assert not recovered["continuation"] and recovered["coverage"] == "ok"
    assert (
        p.db.all("SELECT body FROM reports WHERE report_id='paper_weekly-2026-10-26'")[0]["body"].count(
            "\n### "
        )
        == 5
    )
    p.db.close()
