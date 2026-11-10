"""Build actual public distributions with private-file sentinels in an isolated tree."""

from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_PATHS = (
    ".env",
    "private/positions.csv",
    "config/watchlist.yaml",
    "config/custom-untracked.yaml",
    "config/watchlist.private.yaml",
    "output/premarket.md",
    "data/intelligence.sqlite3",
    "src/investment_intelligence/fixtures/__pycache__/dirty.cpython-312.pyc",
    "src/investment_intelligence/fixtures/news/local.private.xml",
)


def public_project_copy(root, destination):
    shutil.copytree(
        root,
        destination,
        ignore=shutil.ignore_patterns(
            ".git",
            ".env",
            ".env.local",
            "venv",
            ".venv",
            ".pytest_cache",
            "__pycache__",
            "*.egg-info",
            "dist",
            "build",
            "data",
            "output",
            "private",
            "*.private.*",
            "watchlist.yaml",
        ),
    )
    sentinel = secrets.token_bytes(28)
    for relpath in PRIVATE_PATHS:
        path = destination / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(sentinel)
    return sentinel


def test_source_and_wheel_exclude_private_state(tmp_path):
    project = tmp_path / "source"
    sentinel = public_project_copy(ROOT, project)
    dist = tmp_path / "distributions"
    dist.mkdir()
    # Use the project's declared PEP 517 backend. This test can run with networking disabled.
    command = [
        sys.executable,
        "-c",
        "import sys; from setuptools import build_meta; " "getattr(build_meta, sys.argv[1])(sys.argv[2])",
    ]
    subprocess.run(command + ["build_sdist", str(dist)], cwd=project, check=True, capture_output=True)
    [sdist] = dist.glob("*.tar.gz")
    with tarfile.open(sdist) as archive:
        names = []
        for info in archive.getmembers():
            if not info.isfile():
                continue
            assert sentinel not in archive.extractfile(info).read(), info.name
            names.append(info.name.split("/", 1)[1])
        assert "src/investment_intelligence/schema.sql" in names
        assert "config/watchlist.example.yaml" in names
        assert "skills/investment-intelligence/SKILL.md" in names
        assert ".github/workflows/pipeline.yml" in names
        assert all(p not in names for p in PRIVATE_PATHS)
        # A wheel built from the published sdist must still contain its runtime inputs.
        archive.extractall(tmp_path / "unpacked", filter="data")
    [published] = (tmp_path / "unpacked").iterdir()
    subprocess.run(command + ["build_wheel", str(dist)], cwd=published, check=True, capture_output=True)
    [wheel] = dist.glob("*.whl")
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        for name in names:
            assert sentinel not in archive.read(name), name
        assert not any("__pycache__" in name for name in names)
        assert any(n.endswith("/config/watchlist.example.yaml") for n in names)
        assert "investment_intelligence/schema.sql" in names
        assert any(n.endswith("/skills/investment-intelligence/SKILL.md") for n in names)
        for fixture in (
            "corporate_demo.py",
            "news/google.xml",
            "macro/schedule.ics",
            "research/papers-0.xml",
        ):
            assert "investment_intelligence/fixtures/" + fixture in names
