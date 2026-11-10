from argparse import ArgumentParser
from datetime import datetime, timezone
import json
import os
import sys
import time

from .configuration import Settings, read_environment
from .delivery import send_test_email
from .demo import run_demo
from .models import encode
from .pipeline import Pipeline, health_text
from .schedule import JOBS
from .storage import Database


def parser():
    p = ArgumentParser(
        prog="investment-intelligence", description="Source-linked investment research collector"
    )
    subs = p.add_subparsers(dest="command", required=True)
    demo = subs.add_parser("demo", help="Run the four offline fixture pipelines in a separate database")
    demo.add_argument("--output", default=os.getenv("OUTPUT_DIR", "output"))
    run = subs.add_parser("run", help="Run one live job")
    run.add_argument("job", choices=JOBS)
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and parse using a rollback-only transaction; skip delivery and models",
    )
    run.add_argument("--backfill", action="store_true", help="Replay the adapter's bounded source history")
    run.add_argument("--source", help="Restrict this job to one configured source ID")
    subs.add_parser("dispatch", help="Run due local slots and registered events once")
    subs.add_parser("serve", help="Dispatch every minute")
    subs.add_parser("doctor", help="Show persisted source and delivery health")
    subs.add_parser("test-email", help="Send one test message using this deployment's mail settings")
    q = subs.add_parser("search", help="Full-text search event titles and factual summaries")
    q.add_argument("query")
    q.add_argument("--limit", type=int, default=20)
    h = subs.add_parser("history", help="Read evidence revisions, including source facts")
    h.add_argument("event_key")
    r = subs.add_parser("reports", help="List archived Markdown reports or print one")
    r.add_argument("report_id", nargs="?")
    return p


def run(args):
    if args.command == "demo":
        return run_demo(args.output)
    if args.command == "test-email":
        return send_test_email(read_environment())
    settings = Settings()
    db = Database(settings.url, preview=getattr(args, "dry_run", False))
    try:
        if args.command == "search":
            return [
                {
                    "event_key": r["event_key"],
                    "title": r["title"],
                    "status": r["status"],
                    "event_at": r["event_at"],
                }
                for r in db.search(args.query, max(1, min(100, args.limit)))
            ]
        if args.command == "history":
            items = db.all(
                """SELECT d.* FROM event_documents ed JOIN documents d ON d.document_id=ed.document_id
                WHERE ed.event_key=:k ORDER BY d.first_seen_at,d.version,d.document_id""",
                k=args.event_key,
            )
            for item in items:
                item["payload"] = json.loads(item["payload"])
            return items
        if args.command == "reports":
            if args.report_id:
                rows = db.all("SELECT body FROM reports WHERE report_id=:r", r=args.report_id)
                if not rows:
                    raise ValueError("No such report")
                return rows[0]["body"]
            return db.all(
                "SELECT report_id,created_at,mail_state FROM reports ORDER BY created_at DESC, report_id"
            )
        db.register(settings.watchlist["assets"], settings.sources)
        if args.command == "doctor":
            return health_text(db, settings, datetime.now(timezone.utc))
        pipeline = Pipeline(db, settings)
        if args.command == "serve":
            while True:
                try:
                    with db.run_lock():
                        result = pipeline.dispatch(datetime.now(timezone.utc))
                    if result:
                        print(encode(result), flush=True)
                except RuntimeError as exc:
                    if str(exc) != "Another pipeline is running":
                        raise
                time.sleep(60)
        with db.run_lock():
            now = datetime.now(timezone.utc)
            if args.command == "dispatch":
                return pipeline.dispatch(now)
            if args.source and not any(
                s["id"] == args.source and (args.job in s["jobs"] or args.job == "source_health")
                for s in settings.sources
            ):
                raise ValueError("Source is not bound to the requested job")
            return pipeline.run(args.job, now, backfill=args.backfill, only=args.source)
    finally:
        db.close()


def main(argv=None):
    try:
        result = run(parser().parse_args(argv))
        print(
            result
            if isinstance(result, str)
            else json.dumps(result, indent=2, ensure_ascii=False, default=str)
        )
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        from sqlalchemy.exc import SQLAlchemyError

        message = type(exc).__name__ if isinstance(exc, SQLAlchemyError) else str(exc)
        print(f"Error: {message}", file=sys.stderr)
        return 1
