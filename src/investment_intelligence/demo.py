"""Air-gapped rehearsal with a synthetic clock and explicit transport injection."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import tempfile

from .configuration import Settings
from .delivery import NAMES, deliver, snapshot
from .http import FixtureHttp
from .pipeline import ORDER, Pipeline
from .schedule import week_window
from .storage import Database


def run_demo(output):
    settings = Settings({"OUTPUT_DIR": str(Path(output) / "demo")}, demo=True)
    settings.watchlist["managers"] = [{"name": "Situational Awareness LP", "cik": "0002045724"}]
    settings.output.mkdir(parents=True, exist_ok=True)
    clock = datetime(2026, 11, 2, 14, 10, tzinfo=timezone.utc)
    summaries = []
    with tempfile.TemporaryDirectory(dir=settings.output) as tmp:
        dbfile = Path(tmp) / "demo.sqlite3"
        db = Database("sqlite:///" + str(dbfile))
        try:
            http = FixtureHttp()
            pipeline = Pipeline(db, settings, http)
            for phase, jobs in enumerate(
                [
                    ("calendar_sync", "sec_poll", "news_scan", "crypto_scan", "paper_weekly"),
                    ("event_check", "sec_poll", "news_scan", "crypto_scan", "paper_weekly"),
                    ("calendar_sync", "sec_poll", "paper_weekly", "premarket_brief"),
                ]
            ):
                http.phase = phase
                for job in jobs:
                    summaries.append(pipeline.run(job, clock + timedelta(minutes=phase)))
            modules = {}
            for module, title in NAMES.items():
                if module == "research":
                    a, b = week_window(clock, settings.tz)
                    events = db.all(
                        "SELECT * FROM events WHERE module='research' AND event_at>=:a AND event_at<:b"
                        + ORDER
                        + " LIMIT 5",
                        a=a,
                        b=b,
                    )
                else:
                    events = db.all("SELECT * FROM events WHERE module=:m" + ORDER, m=module)
                if not events:
                    raise RuntimeError(f"Offline demo produced no {module} events")
                rid = "demo-" + module
                snapshot(
                    db,
                    settings,
                    rid,
                    module + ".md",
                    events,
                    title + " · demonstration",
                    clock + timedelta(minutes=2),
                )
                filename, _ = deliver(db, settings, rid)
                modules[module] = {"events_in_brief": len(events), "file": filename}
            total = db.all("SELECT COUNT(*) AS n FROM events")[0]["n"]
            docs = db.all("SELECT COUNT(*) AS n FROM documents")[0]["n"]
        finally:
            db.close()
        os.replace(dbfile, settings.output / "demo.sqlite3")
    return {
        "mode": "offline synthetic fixtures",
        "modules": modules,
        "events": total,
        "document_versions": docs,
        "database": str(settings.output / "demo.sqlite3"),
        "warnings": sorted({w for s in summaries for w in s["warnings"]}),
    }
