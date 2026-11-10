"""Package-level regression checks for private state and workflow boundaries."""

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
    assert pipeline["on"]["schedule"] == [{"cron": "7,22,37,52 * * * *"}]
    assert pipeline["jobs"]["collect"]["permissions"] == {"contents": "write", "issues": "write"}
    guard = pipeline["jobs"]["collect"]["if"]
    assert "default_branch" in guard
    assert "github.event_name == 'workflow_dispatch' || vars.ENABLE_SCHEDULE == 'true'" in guard
    assert pipeline["concurrency"]["cancel-in-progress"] is False
    choices = pipeline["on"]["workflow_dispatch"]["inputs"]["job"]["options"]
    assert set(choices) == set(JOBS) | {"dispatch", "doctor", "test-email"}


def test_compose_persists_and_mounts_readonly_configuration():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    service = compose["services"]["intelligence"]
    assert service["restart"] == "unless-stopped"
    assert set(compose["volumes"]) == {"intelligence-data", "intelligence-output"}
    assert "intelligence-data:/srv/intelligence/data" in service["volumes"]
    assert "./config:/srv/intelligence/config:ro" in service["volumes"]
