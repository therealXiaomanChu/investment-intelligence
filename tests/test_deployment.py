"""Package-level regression checks for private state and workflow boundaries."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

from investment_intelligence.configuration import Settings
from investment_intelligence.schedule import JOBS

ROOT = Path(__file__).resolve().parents[1]


def test_cloud_refuses_ephemeral_database():
    with pytest.raises(ValueError, match="PostgreSQL"):
        Settings({"GITHUB_ACTIONS": "true", "DATABASE_URL": "sqlite:///ephemeral.sqlite3"})
    config = Settings({"GITHUB_ACTIONS": "true", "DATABASE_URL": "postgresql://db.example/intel"})
    assert config.url.startswith("postgresql://")


def test_private_content_excluded_from_git(tmp_path):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    shutil.copy(ROOT / ".gitignore", tmp_path / ".gitignore")
    private = [
        ".env",
        ".env.local",
        "config/watchlist.private.yaml",
        "config/watchlist.yaml",
        "private/holdings.csv",
        "data/intelligence.sqlite3",
        "portfolio.db",
        "output/brief.md",
    ]
    result = subprocess.run(
        ["git", "check-ignore", "--stdin"],
        input="\n".join(private + [".env.example"]),
        text=True,
        capture_output=True,
        cwd=tmp_path,
        check=True,
    )
    assert set(result.stdout.splitlines()) == set(private)


def test_actions_scope_and_dispatch_commands():
    path = ROOT / ".github/workflows"
    for file in path.glob("*.yml"):
        workflow = yaml.safe_load(file.read_text())
        assert workflow["permissions"] == {"contents": "read"}
        assert "pull_request_target" not in workflow["on"]
    pipeline = yaml.safe_load((path / "pipeline.yml").read_text())
    [schedule] = pipeline["on"]["schedule"]
    field, *other_fields = schedule["cron"].split()
    assert other_fields == ["*"] * 4
    minutes = list(range(0, 60, 15)) if field == "*/15" else sorted(map(int, field.split(",")))
    assert 0 <= minutes[0] < 15 and minutes == list(range(minutes[0], 60, 15))
    assert pipeline["jobs"]["collect"]["permissions"] == {"contents": "write", "issues": "write"}
    guard = pipeline["jobs"]["collect"]["if"]
    assert guard == (
        "(github.event_name == 'schedule' && vars.ENABLE_SCHEDULE == 'true') || "
        "(github.event_name == 'workflow_dispatch' && github.ref == "
        "format('refs/heads/{0}', github.event.repository.default_branch))"
    )
    assert pipeline["concurrency"]["cancel-in-progress"] is False
    choices = pipeline["on"]["workflow_dispatch"]["inputs"]["job"]["options"]
    assert set(choices) == set(JOBS) | {"dispatch", "doctor", "test-email"}


@pytest.mark.parametrize(
    ("hint", "api", "expected"),
    [
        ("true", "offline", "true"),
        ("false", "offline", "false"),
        ("", "true", "true"),
        ("", "false", "false"),
        ("", "offline", None),
        ("", "null", None),
    ],
)
def test_actions_visibility_lookup(tmp_path, hint, api, expected):
    pipeline = yaml.safe_load((ROOT / ".github/workflows/pipeline.yml").read_text())
    step = next(
        s for s in pipeline["jobs"]["collect"]["steps"] if s.get("name") == "Resolve repository visibility"
    )
    gh = tmp_path / "gh"
    gh.write_text(
        "#!/bin/sh\n[ \"$*\" = 'api repos/owner/project --jq .private' ] && "
        '[ "$API_REPLY" != offline ] && echo "$API_REPLY"\n'
    )
    gh.chmod(0o700)
    target = tmp_path / "published.env"
    env = {
        **os.environ,
        "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
        "EVENT_PRIVATE": hint,
        "API_REPLY": api,
        "GITHUB_REPOSITORY": "owner/project",
        "GITHUB_ENV": str(target),
    }
    run = subprocess.run(["bash", "-e", "-c", step["run"]], env=env, capture_output=True)
    if expected is None:
        assert run.returncode != 0 and not target.exists()
    else:
        assert run.returncode == 0 and target.read_text().strip() == f"REPOSITORY_PRIVATE={expected}"


def test_compose_persists_and_mounts_readonly_configuration():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    service = compose["services"]["intelligence"]
    assert service["restart"] == "unless-stopped"
    assert set(compose["volumes"]) == {"intelligence-data", "intelligence-output"}
    assert "intelligence-data:/srv/intelligence/data" in service["volumes"]
    assert "./config:/srv/intelligence/config:ro" in service["volumes"]
