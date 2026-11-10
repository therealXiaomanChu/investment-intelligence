from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
import os
import sys
from zoneinfo import ZoneInfo

import yaml
from dotenv import dotenv_values

from .delivery import mail_environment


def read_environment(env=None):
    return mail_environment(env if env is not None else {**dotenv_values(".env"), **os.environ})


def load_config(path):
    with open(path, encoding="utf-8") as handle:
        value = yaml.safe_load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"Expected a mapping in {path}")
    return value


class Settings:
    def __init__(self, env=None, *, demo=False):
        self.env = read_environment(env)
        default = Path(__file__).resolve().parents[2] / "config"
        if not default.joinpath("sources.yaml").exists():
            default = Path(sys.prefix) / "share/investment-intelligence/config"
        self.directory = Path(self.env.get("CONFIG_DIR", default)) if not demo else default
        self.output = Path(self.env.get("OUTPUT_DIR", "output"))
        self.url = self.env.get("DATABASE_URL") or "sqlite:///data/intelligence.sqlite3"
        if (
            not demo
            and self.env.get("GITHUB_ACTIONS") == "true"
            and not self.url.startswith(("postgres://", "postgresql://", "postgresql+psycopg://"))
        ):
            state = self.env.get("CLOUD_STATE_DIR")
            if (
                self.env.get("DATABASE_URL")
                or not state
                or not (Path(state) / "head").is_file()
                or not (Path(state) / "intelligence.sqlite3").is_file()
            ):
                raise ValueError(
                    "GitHub Actions requires restored SQLite state or an external PostgreSQL DATABASE_URL"
                )
            self.url = "sqlite:///" + str((Path(state) / "intelligence.sqlite3").resolve())
        self.tz = ZoneInfo(self.env.get("TIMEZONE", "America/New_York"))
        self.limit = int(self.env.get("FETCH_LIMIT", "100"))
        if not 1 <= self.limit <= 1000:
            raise ValueError("FETCH_LIMIT must be between 1 and 1000")
        self.sources = self.config("sources")["sources"]
        watchfile = self.env.get("WATCHLIST_FILE") if not demo else None
        self.watchlist = load_config(watchfile) if watchfile else self.config("watchlist.example")
        if not isinstance(self.watchlist.get("assets"), list):
            raise ValueError("Watchlist requires an assets list")
        for records in (self.watchlist["assets"], self.sources):
            if len({r["id"] for r in records}) != len(records):
                raise ValueError("Duplicate ID in configuration")
        self.demo = demo

    def config(self, name):
        return load_config(self.directory / f"{name}.yaml")


@dataclass
class Context:
    settings: Settings
    now: object
    http: object
    job: str
    backfill: bool
    cursor: dict
    due: list
    warnings: list = field(default_factory=list)

    def __post_init__(self):
        self.next_cursor = deepcopy(self.cursor)
        self.watchlist = self.settings.watchlist
        self.env = self.settings.env
        self.limit = self.settings.limit
        self.demo = self.settings.demo
        self.config = self.settings.config

    def warn(self, message):
        if message not in self.warnings:
            self.warnings.append(str(message)[:400])
