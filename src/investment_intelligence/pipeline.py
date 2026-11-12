"""Job orchestration. Each source commits its observations and successful cursor together."""

from datetime import datetime, timedelta
from importlib import import_module
import json
from zoneinfo import ZoneInfo

from sqlalchemy.exc import SQLAlchemyError

from . import analysis
from .configuration import Context
from .delivery import deliver, snapshot, plain
from .http import Http
from .models import digest, encode, utc
from .schedule import JOBS, eligible, week_window
from .storage import EVIDENCE_URLS_SQL, execute, first, rows

ORDER = " ORDER BY importance DESC,event_at DESC,event_key"


def same_release_values(conn, event):
    """A publication timestamp or supporting URL can arrive after an alert."""
    previous = first(
        conn,
        "SELECT event_data FROM alert_queue WHERE event_key=:k ORDER BY event_version DESC LIMIT 1",
        k=event["event_key"],
    )
    if not previous:
        return False
    prior_doc = json.loads(previous["event_data"])["current_document"]
    if prior_doc == event["current_document"]:
        return True
    values = (
        "SELECT metric,value,unit,period FROM facts WHERE document_id=:d ORDER BY metric,period,unit,value"
    )
    return rows(conn, values, d=prior_doc) == rows(conn, values, d=event["current_document"])


class Pipeline:
    def __init__(self, db, settings, http=None):
        self.db, self.settings = db, settings
        self.http = http or Http(settings.env)
        db.register(settings.watchlist["assets"], settings.sources)

    def due(self, now, *, recovery=False):
        if recovery:
            condition = "status='delayed' AND next_check IS NULL AND updated_at>=:cut"
        else:
            condition = "next_check IS NOT NULL AND next_check<=:now"
        records = self.db.all(
            "SELECT * FROM events WHERE " + condition + " ORDER BY event_at,event_key",
            now=utc(now),
            cut=utc(now - timedelta(days=7)),
        )
        for row in records:
            row["metadata"] = json.loads(row["metadata"])
        return records

    def collect(self, source, job, now, backfill, due):
        db = self.db
        ident = source["id"]
        stored = db.all("SELECT cursor FROM sources WHERE source_id=:s", s=ident)[0]
        ctx = Context(
            self.settings, now, self.http, job, backfill, json.loads(stored["cursor"]), due, archive=db
        )
        changes = set()
        observed_at = utc(now)
        allow_alerts = not backfill and job not in {"source_health", "paper_weekly"}
        cutoff = utc(now - timedelta(days=2))
        try:
            module = import_module(".collectors." + source["module"], __package__)
            records = list(module.collect(ctx, source))
            with db.write() as conn:
                for record in records:
                    if record.source_id != ident:
                        raise ValueError(f"Unexpected source identity from {ident}")
                    record = db.canonical_record(conn, record)
                    if not db.ingest(conn, record, observed_at):
                        continue
                    changes.add(record.event_key)
                    official_release = (
                        ident in {"bls_actuals", "bea_releases"}
                        and bool(record.facts)
                        and bool(record.metadata.get("publication_evidence"))
                        and record.published_at is not None
                        and cutoff <= record.published_at <= observed_at
                    )
                    if not allow_alerts or (job == "calendar_sync" and not official_release):
                        continue
                    current = first(
                        conn,
                        """SELECT * FROM events WHERE event_key=:k AND event_at>=:cut AND importance>=0.85
                           AND status IN ('published','released','revised','postponed')""",
                        k=record.event_key,
                        cut=cutoff,
                    )
                    if current is None:
                        continue
                    if official_release and same_release_values(conn, current):
                        continue
                    current["fallback_signals"] = rows(
                        conn,
                        "SELECT * FROM signals WHERE event_key=:k ORDER BY asset_id",
                        k=record.event_key,
                    )
                    current["alert_urls"] = list(
                        execute(conn, EVIDENCE_URLS_SQL, key=record.event_key).scalars()
                    )
                    aid = "alert-" + digest([record.event_key, current["version"]])[:24]
                    execute(
                        conn,
                        """INSERT INTO alert_queue(alert_id,event_key,event_version,event_data,created_at)
                           VALUES(:id,:k,:v,:data,:t) ON CONFLICT DO NOTHING""",
                        id=aid,
                        k=record.event_key,
                        v=current["version"],
                        data=encode(current),
                        t=observed_at,
                    )
                health = "partial" if ctx.warnings else "ok"
                if ctx.warnings and not records and ctx.next_cursor == ctx.cursor:
                    health = "unavailable"
                execute(
                    conn,
                    """UPDATE sources SET last_attempt=:t,
                    last_success=CASE WHEN :ok=1 THEN :t ELSE last_success END,health=:h,
                    last_error=:e,cursor=:c WHERE source_id=:s""",
                    t=utc(now),
                    ok=int(health != "unavailable"),
                    h=health,
                    e=" | ".join(ctx.warnings)[:1500] or None,
                    c=encode(ctx.next_cursor),
                    s=ident,
                )
            return changes, ctx.warnings
        except SQLAlchemyError:
            raise
        except Exception as exc:
            # Public diagnostics omit secrets and database connection strings.
            msg = str(exc)[:350] if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
            for key, value in self.settings.env.items():
                if value and any(k in key for k in ("KEY", "PASSWORD", "DATABASE_URL", "TOKEN")):
                    msg = msg.replace(str(value), "[redacted]")
            with db.write() as conn:
                execute(
                    conn,
                    "UPDATE sources SET last_attempt=:t,health='unavailable',last_error=:e WHERE source_id=:s",
                    t=utc(now),
                    e=msg,
                    s=ident,
                )
            return set(), [msg]

    def mark_attempts(self, due, now):
        with self.db.write() as conn:
            for row in due:
                current = first(
                    conn, "SELECT next_check,attempts FROM events WHERE event_key=:k", k=row["event_key"]
                )
                if not current or not current["next_check"] or current["next_check"] > utc(now):
                    continue
                attempts = current["attempts"] + 1
                scheduled = row["metadata"].get("check_at") or row["scheduled_at"]
                expired = attempts >= 6 or now >= datetime.fromisoformat(utc(scheduled)) + timedelta(hours=3)
                next_at = None if expired else utc(now + timedelta(minutes=min(15, 5 * attempts)))
                execute(
                    conn,
                    """UPDATE events SET attempts=:a,next_check=:next,
                    status=CASE WHEN :end=1 THEN 'delayed' ELSE status END,
                    updated_at=CASE WHEN :end=1 THEN :now ELSE updated_at END WHERE event_key=:k""",
                    a=attempts,
                    next=next_at,
                    end=int(expired),
                    now=utc(now),
                    k=row["event_key"],
                )

    def run(self, job, now, *, backfill=False, only=None, discover_bls=False):
        if job not in JOBS:
            raise ValueError(f"Unknown job {job}")
        db, settings = self.db, self.settings
        notes, changed, reports = [], set(), []
        due = (
            self.due(now, recovery=job == "source_health") if job in {"event_check", "source_health"} else []
        )
        if job == "calendar_sync":
            for row in db.all(
                """SELECT * FROM events WHERE module='macro' AND status IN ('published','revised','released')
                    AND event_at>=:cut""",
                cut=utc(now - timedelta(days=90)),
            ):
                row["metadata"] = json.loads(row["metadata"])
                due.append(row)
        if job == "source_health":
            health = {r["source_id"]: r for r in db.all("SELECT * FROM sources")}
        for source in settings.sources:
            sid = source["id"]
            if only and sid != only:
                continue
            relevant = [e for e in due if e["metadata"].get("check_source") == sid]
            work_job = job
            if job == "source_health":
                old = health[sid]
                when = old["last_success"]
                stale = bool(
                    when
                    and now - datetime.fromisoformat(when) > timedelta(hours=source.get("stale_hours", 48))
                )
                if relevant:
                    work_job = "event_check"
                elif (old["health"] in {"unavailable", "partial"} or stale) and source["jobs"] != [
                    "event_check"
                ]:
                    work_job = source["jobs"][0]
                else:
                    continue
            elif job == "event_check" and discover_bls and sid == "bls_actuals" and job in source["jobs"]:
                work_job = "bls_probe"
            elif job not in source["jobs"] or (job == "event_check" and not relevant):
                continue
            recover_backfill = job == "source_health"
            if recover_backfill and sid == "arxiv":
                recover_backfill = json.loads(health[sid]["cursor"]).get("mode") == "backfill"
            keys, warnings = self.collect(source, work_job, now, backfill or recover_backfill, relevant)
            changed.update(keys)
            notes.extend(f"{sid}: {w}" for w in warnings)
        if job == "event_check":
            self.mark_attempts([e for e in due if not only or e["metadata"].get("check_source") == only], now)
        paper_incomplete = False
        previous_window = False
        if job == "paper_weekly":
            prior = db.all("SELECT cursor,health FROM sources WHERE source_id='arxiv'")
            arxiv_cursor = json.loads(prior[0]["cursor"]) if prior else {}
            paper_incomplete = bool(
                prior and (prior[0]["health"] == "unavailable" or arxiv_cursor.get("incomplete"))
            )
            if arxiv_cursor.get("end"):
                previous_window = utc(arxiv_cursor["end"]) != week_window(now, settings.tz)[1]
        if (
            not db.preview
            and not paper_incomplete
            and job not in {"calendar_sync", "source_health"}
            and not (discover_bls and not due)
        ):
            try:
                analysis.analyze(db, settings, now, job)
            except (ValueError, KeyError, ArithmeticError) as exc:
                notes.append(f"Model configuration unavailable ({type(exc).__name__}); facts preserved")
        stamp = now.astimezone(settings.tz).date().isoformat()
        note = (
            "\n".join(["Source coverage is partial.", ""] + ["- " + plain(s) for s in notes]) if notes else ""
        )
        if job == "source_health":
            note = health_text(db, settings, now)
        if job in {"premarket_brief", "after_close", "paper_weekly", "source_health"}:
            if job == "paper_weekly":
                start, end = week_window(now, settings.tz)
                if previous_window:
                    end = utc(arxiv_cursor["end"])
                    start = utc(datetime.fromisoformat(end).astimezone(settings.tz) - timedelta(days=7))
                events = db.all(
                    "SELECT * FROM events WHERE module='research' AND event_at>=:a AND event_at<:b"
                    + ORDER
                    + " LIMIT 5",
                    a=start,
                    b=end,
                )
                stamp = datetime.fromisoformat(start).astimezone(settings.tz).date().isoformat()
                title = "Weekly research · top five · week of " + stamp
                if paper_incomplete:
                    stamp += "-partial"
                    title += " (scan in progress)"
            elif job == "source_health":
                events, title = [], "Source health · " + stamp
            else:
                # A rerun of an already delivered slot replays the immutable report.
                older = db.all(
                    """SELECT MAX(created_at) AS t FROM reports WHERE report_id LIKE :prefix
                    AND report_id<>:current""",
                    prefix=job + "-%",
                    current=job + "-" + stamp,
                )
                cutoff = older[0]["t"] or utc(now - timedelta(days=3))
                local = now.astimezone(settings.tz).replace(hour=0, minute=0, second=0, microsecond=0)
                events = db.all(
                    """SELECT * FROM events WHERE updated_at<=:b AND (updated_at>:a OR
                    (event_at>=:start AND event_at<:end AND status IN ('scheduled','estimated','confirmed','delayed')))"""
                    + ORDER,
                    a=cutoff,
                    b=utc(now),
                    start=utc(local),
                    end=utc(local + timedelta(days=1)),
                )
                title = (
                    ("Pre-market brief" if job == "premarket_brief" else "After-close brief") + " · " + stamp
                )
            rid = f"{job}-{stamp}"
            reports.append(
                snapshot(
                    db, settings, rid, rid + ".md", events, title, now, note, provisional=paper_incomplete
                )
            )
        for pending in db.all(
            "SELECT * FROM alert_queue WHERE report_id IS NULL ORDER BY created_at,alert_id"
        ):
            event = json.loads(pending["event_data"])
            rid = pending["alert_id"]
            snapshot(db, settings, rid, rid + ".md", [event], "Material update · " + event["title"], now)
            with db.write() as conn:
                execute(conn, "UPDATE alert_queue SET report_id=:id WHERE alert_id=:id", id=rid)
            reports.append(rid)
        artifacts = []
        if not db.preview:
            # A crash after persisting a report leaves it eligible for local and SMTP recovery.
            for row in db.all(
                """SELECT r.report_id FROM reports r LEFT JOIN file_deliveries f ON f.report_id=r.report_id
                    WHERE r.mail_state='ready' OR f.report_id IS NULL"""
            ):
                if row["report_id"] not in reports:
                    reports.append(row["report_id"])
            for rid in reports:
                filename, state = deliver(db, settings, rid)
                artifacts.append({"file": filename, "email": state})
                if state in {"sending", "uncertain", "configuration_error"}:
                    channel = (
                        "GitHub notification" if settings.env.get("EMAIL_TRANSPORT") == "github" else "SMTP"
                    )
                    notes.append(f"{rid} {channel} outcome requires inspection")
        with db.write() as conn:
            execute(
                conn,
                """INSERT INTO jobs(job_name,last_run,last_success,last_error,retry_count) VALUES(:j,:t,CASE WHEN :n=0 THEN :t ELSE NULL END,:err,:n)
                ON CONFLICT(job_name) DO UPDATE SET last_run=:t,last_success=CASE WHEN :n=0 THEN :t ELSE jobs.last_success END,last_error=:err,
                retry_count=CASE WHEN :n=0 THEN 0 ELSE jobs.retry_count+1 END""",
                j=job,
                t=utc(now),
                err=" | ".join(notes)[:1500] or None,
                n=int(bool(notes)),
            )
        return {
            "job": job,
            "changed_events": len(changed),
            "coverage": "partial" if notes else "ok",
            "warnings": notes,
            "reports": artifacts,
            "preview": db.preview,
            "continuation": paper_incomplete or previous_window,
        }

    def claim_bls_probe(self, now):
        local = now.astimezone(ZoneInfo("America/New_York"))
        elapsed = local.hour * 60 + local.minute - (8 * 60 + 35)
        if local.weekday() >= 5 or not 0 <= elapsed < 90:
            return False
        slot = utc(local.replace(second=0, microsecond=0) - timedelta(minutes=elapsed % 15))
        # A crash must not spend another quota unit on the same optional query.
        with self.db.write() as conn:
            result = execute(
                conn,
                """INSERT INTO slots(job_name,slot)
                SELECT 'bls_probe', :slot WHERE EXISTS (SELECT 1 FROM sources
                WHERE source_id='bls_calendar' AND health IN ('unavailable', 'partial'))
                ON CONFLICT DO NOTHING""",
                slot=slot,
            )
            return result.rowcount == 1

    def dispatch(self, now):
        slots = eligible(now, self.settings.tz)
        results = []
        for job in JOBS:
            if job == "event_check":
                probe = self.claim_bls_probe(now)
                if probe or self.due(now):
                    results.append(self.run(job, now, discover_bls=probe))
                continue
            if job not in slots:
                continue
            slot = slots[job]
            if self.db.all("SELECT 1 FROM slots WHERE job_name=:j AND slot=:s", j=job, s=slot):
                continue
            if job == "paper_weekly":
                recent = self.db.all("SELECT last_run FROM jobs WHERE job_name=:j", j=job)
                status = self.db.all("SELECT health FROM sources WHERE source_id='arxiv'")
                delay = 120 if status and status[0]["health"] == "unavailable" else 10
                if recent and now - datetime.fromisoformat(recent[0]["last_run"]) < timedelta(minutes=delay):
                    continue
            result = self.run(job, now)
            results.append(result)
            if result["continuation"]:
                continue
            # Incomplete arXiv pagination is picked up in a subsequent dispatch before delivery.
            with self.db.write() as conn:
                execute(conn, "INSERT INTO slots VALUES(:j,:s) ON CONFLICT DO NOTHING", j=job, s=slot)
        return results


def health_text(db, settings, now):
    states = {r["source_id"]: r for r in db.all("SELECT * FROM sources")}
    lines = ["| Connector | Health | Last success (UTC) | Detail |", "|---|---|---|---|"]
    for s in settings.sources:
        r = states[s["id"]]
        health = r["health"]
        if (
            health == "ok"
            and r["last_success"]
            and now - datetime.fromisoformat(r["last_success"]) > timedelta(hours=s.get("stale_hours", 48))
        ):
            health = "stale"
        detail = r["last_error"] or ""
        if s["id"] in {"sec_filings", "sec_13f", "earnings_results"} and not settings.env.get(
            "SEC_USER_AGENT"
        ):
            detail = "SEC_USER_AGENT required for SEC requests"
        lines.append(f"| {s['id']} | {health} | {r['last_success'] or '—'} | {plain(detail)} |")
    github = settings.env.get("EMAIL_TRANSPORT") == "github"
    mail_key = "NOTIFICATION_GITHUB_TOKEN" if github else "SMTP_HOST"
    for key in ("OPENALEX_API_KEY", "BLS_API_KEY", "FRED_API_KEY", "LLM_API_KEY", mail_key):
        lines.append(f"\n{key} {'configured' if settings.env.get(key) else 'absent'}")
    delayed = db.all("SELECT event_key FROM events WHERE status='delayed' AND next_check IS NULL")
    if delayed:
        lines.append("\nExhausted release checks: " + ", ".join(r["event_key"] for r in delayed))
    pending = db.all(
        "SELECT report_id,mail_state FROM reports WHERE mail_state IN ('uncertain','sending') OR (mail_state='ready' AND mail_error IS NOT NULL)"
    )
    for r in pending:
        lines.append(
            f"\n{'GitHub notification' if github else 'SMTP'} inspection required for {r['report_id']} ({r['mail_state']})"
        )
    return "\n".join(lines)
