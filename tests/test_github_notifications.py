from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from investment_intelligence import cli, cloud_state, delivery
from investment_intelligence.configuration import Settings
from investment_intelligence.pipeline import Pipeline
from investment_intelligence.storage import Database, execute


REPO = "owner/monitor"
CONTEXT = {
    "EMAIL_TRANSPORT": "github",
    "GITHUB_ACTIONS": "true",
    "GITHUB_SERVER_URL": "https://github.com",
    "GITHUB_REPOSITORY": REPO,
    "GITHUB_REPOSITORY_OWNER": "owner",
    "GITHUB_RUN_ID": "123",
    "NOTIFICATION_GITHUB_TOKEN": "test-token",
}


class GitHub:
    def __init__(self):
        self.info = {
            "full_name": REPO,
            "owner": {"type": "User", "login": "owner"},
            "private": True,
            "has_issues": True,
        }
        self.issues = []
        self.before_post = lambda: None
        self.timeout = False

    def request(self, method, url, *, headers, allow_redirects, timeout, **kwargs):
        base = "https://api.github.com/repos/" + REPO
        assert headers["Authorization"] == "Bearer test-token" and not allow_redirects
        if method == "GET" and url == base:
            data = self.info
        elif method == "GET" and url == base + "/issues":
            assert kwargs["params"]["creator"] == "github-actions[bot]"
            params = kwargs["params"]
            offset = (params["page"] - 1) * params["per_page"]
            data = self.issues[offset : offset + params["per_page"]]
        else:
            assert method == "POST" and url == base + "/issues"
            self.before_post()
            data = {**kwargs["json"], "user": {"login": "github-actions[bot]"}, "repository_url": base}
            self.issues.append(data)
            if self.timeout:
                raise delivery.requests.Timeout()
        return SimpleNamespace(status_code=201 if method == "POST" else 200, json=lambda: data)


def test_test_email_is_db_free_and_reuses_an_existing_issue(monkeypatch, tmp_path):
    api = GitHub()
    monkeypatch.setattr(delivery.requests, "request", api.request)
    for key, value in CONTEXT.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DATABASE_URL", "intentionally-unusable")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["test-email"]) == cli.main(["test-email"]) == 0
    assert len(api.issues) == 1 and api.issues[0]["body"].count("@owner") == 1
    assert "test-email-123" in api.issues[0]["body"] and not list(tmp_path.iterdir())
    api.issues[:0] = [dict(api.issues[0], title="A different alert") for _ in range(100)]
    assert cli.main(["test-email"]) == 0 and len(api.issues) == 101


@pytest.mark.parametrize(
    "overrides",
    [
        {"private": False},
        {"has_issues": False},
        {"owner": {"login": "owner", "type": "Organization"}},
        {"full_name": "owner/another"},
        {"owner": {"type": "User", "login": "another"}},
    ],
)
def test_refuses_wrong_target_before_sending(monkeypatch, overrides):
    api = GitHub()
    api.info.update(overrides)
    monkeypatch.setattr(delivery.requests, "request", api.request)
    with pytest.raises(ValueError, match="private personal repository"):
        delivery.send_test_email(CONTEXT)
    assert not api.issues


def test_missing_actions_context_never_uses_smtp_or_contacts_github(monkeypatch):
    monkeypatch.setattr(delivery.requests, "request", lambda *a, **kw: pytest.fail("Unexpected network call"))
    env = delivery.mail_environment(
        {"EMAIL_TRANSPORT": "github", "GMAIL_ADDRESS": "out@example.test", "GITHUB_RUN_ID": "123"}
    )
    assert "SMTP_HOST" not in env
    with pytest.raises(ValueError, match="Actions"):
        delivery.send_test_email(env)


def test_material_pipeline_claims_before_post_and_reconciles_ambiguous_send(tmp_path, monkeypatch):
    class News:
        def get(self, url, **kwargs):
            return b"""<html><main><article><header><time>Nov 1, 2026</time></header>
                <a href="/news/ipo-test"><h3>IPO update @stranger</h3></a></article></main></html>"""

    api = GitHub()
    api.timeout = True
    monkeypatch.setattr(delivery.requests, "request", api.request)
    state = tmp_path / "state"
    state.mkdir()
    (state / "head").write_text("restored")
    Database("sqlite:///" + str(state / "intelligence.sqlite3")).close()
    settings = Settings({**CONTEXT, "CLOUD_STATE_DIR": str(state), "OUTPUT_DIR": str(tmp_path / "output")})
    db = Database(settings.url)
    checkpoints = []

    def checkpoint(db, env):
        assert db.all("SELECT mail_state FROM reports") == [{"mail_state": "sending"}]
        checkpoints.append(True)

    monkeypatch.setattr(cloud_state, "before_external", checkpoint)
    api.before_post = lambda: checkpoints[-1]
    now = datetime(2026, 11, 2, 15, tzinfo=timezone.utc)
    try:
        pipeline = Pipeline(db, settings, News())
        result = pipeline.run("news_scan", now, only="anthropic_news")
        assert [r["email"] for r in result["reports"]] == ["uncertain"] and checkpoints
        [issue] = api.issues
        assert issue["body"].count("@") == 1 and issue["body"].startswith("@owner\n")
        assert "@stranger" not in issue["title"]
        assert "<https://www.anthropic.com/news/ipo-test>" in issue["body"]
        [row] = db.all("SELECT report_id,mail_state FROM reports")
        rid = row["report_id"]
        assert rid.startswith("alert-") and rid in issue["body"]
        assert delivery.deliver(db, settings, rid)[1] == "uncertain"
        with db.write() as conn:
            execute(conn, "UPDATE reports SET mail_state='ready' WHERE report_id=:id", id=rid)
        assert delivery.deliver(db, settings, rid)[1] == "sent" and len(api.issues) == 1
        delivery.snapshot(db, settings, "premarket-123", "brief.md", [], "Brief", now)
        assert delivery.deliver(db, settings, "premarket-123")[1] == "disabled"
    finally:
        db.close()
