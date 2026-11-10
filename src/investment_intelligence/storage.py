"""Portable SQL, immutable observation versions and serialized write transactions."""

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from importlib.resources import files
from pathlib import Path
import json
import re

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url

from .models import digest, encode, utc


EVIDENCE_URLS_SQL = """SELECT DISTINCT d.canonical_url FROM event_documents ed
    JOIN documents d ON ed.document_id=d.document_id
    WHERE ed.event_key=:key ORDER BY d.canonical_url"""


def execute(conn, sql, **params):
    return conn.execute(text(sql), params)


def rows(conn, sql, **params):
    return [dict(row) for row in execute(conn, sql, **params).mappings()]


def first(conn, sql, **params):
    row = execute(conn, sql, **params).mappings().first()
    return dict(row) if row else None


class Database:
    def __init__(self, url, preview=False):
        self.preview = preview
        self._outer = None
        if url.startswith(("postgres://", "postgresql://")):
            url = "postgresql+psycopg://" + url.split("://", 1)[1]
        parsed = make_url(url)
        self.pg = parsed.get_backend_name() == "postgresql"
        if not self.pg and parsed.get_backend_name() != "sqlite":
            raise ValueError("Use SQLite or PostgreSQL")
        self.path = None
        if not self.pg and parsed.database not in (None, "", ":memory:"):
            self.path = Path(parsed.database).resolve()
            if preview and not self.path.exists():
                url = "sqlite:///:memory:"
                self.path = None
            else:
                self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(url, pool_pre_ping=True, hide_parameters=True)
        if not self.pg:

            @event.listens_for(self.engine, "connect")
            def configure(dbapi, _):
                dbapi.execute("PRAGMA foreign_keys=ON")
                dbapi.execute("PRAGMA busy_timeout=30000")
                if not self.preview:
                    dbapi.execute("PRAGMA journal_mode=WAL")

        if preview:
            self._outer = self.engine.connect()
            if not self.pg:
                self._outer.exec_driver_sql("BEGIN")
        schema = files(__package__).joinpath("schema.sql").read_text()
        with self.write() as conn:
            for stmt in schema.split(";"):
                if stmt.strip():
                    conn.exec_driver_sql(stmt)
            execute(conn, "INSERT INTO schema_versions(version) VALUES (1) ON CONFLICT DO NOTHING")
            if self.pg:
                conn.exec_driver_sql(
                    """CREATE INDEX IF NOT EXISTS events_fts ON events USING gin
                    (to_tsvector('simple',title || ' ' || summary))"""
                )
            else:
                conn.exec_driver_sql(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(event_key UNINDEXED, title, summary)"
                )

    @contextmanager
    def write(self):
        if self._outer is not None:
            with self._outer.begin_nested():
                yield self._outer
            return
        with self.engine.begin() as conn:
            if self.pg:
                conn.exec_driver_sql("SELECT pg_advisory_xact_lock(77901913)")
            else:
                conn.exec_driver_sql("BEGIN IMMEDIATE")
            yield conn

    @contextmanager
    def run_lock(self):
        """OS/session scoped: a crash releases the lock without an expiring lease."""
        if self.preview:
            yield
        elif self.pg:
            with self.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                if not conn.exec_driver_sql("SELECT pg_try_advisory_lock(77901914)").scalar():
                    raise RuntimeError("Another pipeline is running")
                try:
                    yield
                finally:
                    conn.exec_driver_sql("SELECT pg_advisory_unlock(77901914)")
        elif self.path:
            # Local deployment targets POSIX/macOS, Linux or Docker.
            import fcntl

            with open(str(self.path) + ".lock", "a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise RuntimeError("Another pipeline is running") from exc
                yield
        else:
            yield

    def all(self, sql, **params):
        if self._outer is not None:
            return rows(self._outer, sql, **params)
        with self.engine.connect() as conn:
            return rows(conn, sql, **params)

    def close(self):
        if self._outer is not None:
            self._outer.rollback()
            self._outer.close()
        self.engine.dispose()

    def search(self, query, limit):
        words = re.findall(r"\w+", query)[:16]
        if not words:
            return []
        if self.pg:
            sql = """SELECT e.* FROM events e WHERE to_tsvector('simple', title || ' ' || summary)
                     @@ plainto_tsquery('simple', :query) ORDER BY updated_at DESC, event_key LIMIT :limit"""
            query = " ".join(words)
        else:
            sql = """SELECT e.* FROM events_fts f JOIN events e ON e.event_key=f.event_key
                     WHERE events_fts MATCH :query ORDER BY updated_at DESC, e.event_key LIMIT :limit"""
            query = " AND ".join('"' + w + '"' for w in words)
        return self.all(sql, query=query, limit=limit)

    def register(self, assets, sources):
        with self.write() as conn:
            for a in assets:
                execute(
                    conn,
                    """INSERT INTO assets VALUES (:id,:symbol,:issuer,:type,:tier,:cfg)
                    ON CONFLICT(asset_id) DO UPDATE SET symbol=excluded.symbol, issuer_id=excluded.issuer_id,
                    asset_type=excluded.asset_type, watchlist_tier=excluded.watchlist_tier, configuration=excluded.configuration""",
                    id=a["id"],
                    symbol=a["symbol"],
                    issuer=a.get("issuer_id"),
                    type=a["type"],
                    tier=a.get("tier", "watchlist"),
                    cfg=encode(a),
                )
            for s in sources:
                execute(
                    conn,
                    """INSERT INTO sources(source_id,url,module,access_policy,parser_version)
                    VALUES(:id,:url,:module,:access,'1') ON CONFLICT(source_id) DO UPDATE SET url=excluded.url,
                    module=excluded.module, access_policy=excluded.access_policy""",
                    id=s["id"],
                    url=s["url"],
                    module=s["module"],
                    access=s.get("access", "public metadata"),
                )

    @staticmethod
    def canonical_record(conn, record):
        if record.module != "news" or record.kind not in {"news", "ipo_announcement"}:
            return record
        prior = first(
            conn,
            """SELECT e.event_key FROM events e JOIN event_documents ed ON e.event_key=ed.event_key
            JOIN documents d ON ed.document_id=d.document_id WHERE d.canonical_url=:url
            AND e.module='news' AND e.kind IN ('news','ipo_announcement') ORDER BY e.first_seen_at,e.event_key LIMIT 1""",
            url=record.url,
        )
        return replace(record, event_key=prior["event_key"]) if prior else record

    def ingest(self, conn, record, now):
        now = utc(now)
        p = record.payload()
        h = digest(p)
        ident = dict(source=record.source_id, ext=record.external_id)
        last = first(
            conn,
            """SELECT version,content_hash FROM documents WHERE source_id=:source
            AND external_id=:ext ORDER BY version DESC LIMIT 1""",
            **ident,
        )
        if last and last["content_hash"] == h:
            return False
        version = last["version"] + 1 if last else 1
        doc = digest([*ident.values(), version])
        execute(
            conn,
            """INSERT INTO documents VALUES(:doc,:source,:ext,:url,:published,:now,:at,:h,:version,:payload)""",
            **ident,
            doc=doc,
            url=record.url,
            published=record.published_at,
            now=now,
            at=record.event_at,
            h=h,
            version=version,
            payload=encode(p),
        )
        key = record.event_key
        old = first(conn, "SELECT * FROM events WHERE event_key=:key", key=key)
        # Older historical evidence and repeat calendar announcements cannot undo an actual release.
        closed = {"released", "published", "revised", "completed", "canceled", "cancelled"}
        scheduled = {"scheduled", "confirmed", "estimated", "postponed", "delayed"}
        regresses = bool(old and old["status"] in closed and record.status in scheduled)
        older = bool(
            old and record.published_at and old["published_at"] and record.published_at < old["published_at"]
        )
        current = not regresses and not older
        ev_version = old["version"] + 1 if old else 1
        if current:
            retry_at = None
            attempts = 0
            check_at = record.metadata.get("check_at") or record.scheduled_at
            if check_at and record.status in scheduled and record.metadata.get("check_source"):
                retry_at = utc(datetime.fromisoformat(utc(check_at)) + timedelta(minutes=5))
                if old and (json.loads(old["metadata"]).get("check_at") or old["scheduled_at"]) == check_at:
                    retry_at, attempts = old["next_check"], old["attempts"]
            execute(
                conn,
                """INSERT INTO events(event_key,module,kind,event_at,published_at,first_seen_at,updated_at,
                scheduled_at,status,title,summary,importance,metadata,version,current_document,attempts,next_check)
                VALUES(:key,:module,:kind,:at,:pub,:now,:now,:scheduled,:status,:title,:summary,:importance,:meta,:v,:doc,:n,:retry)
                ON CONFLICT(event_key) DO UPDATE SET event_at=excluded.event_at,published_at=excluded.published_at,
                updated_at=excluded.updated_at,scheduled_at=CASE WHEN excluded.status IN ('postponed','delayed','canceled','cancelled') THEN excluded.scheduled_at ELSE COALESCE(excluded.scheduled_at,events.scheduled_at) END,
                status=excluded.status,title=excluded.title,summary=excluded.summary,importance=excluded.importance,
                metadata=excluded.metadata,version=excluded.version,current_document=excluded.current_document,
                attempts=excluded.attempts,next_check=excluded.next_check""",
                key=key,
                module=record.module,
                kind=record.kind,
                at=record.event_at,
                pub=record.published_at,
                now=now,
                scheduled=record.scheduled_at,
                status=record.status,
                title=record.title,
                summary=record.summary,
                importance=record.importance,
                meta=encode(record.metadata),
                v=ev_version,
                doc=doc,
                n=attempts,
                retry=retry_at,
            )
            if not self.pg:
                execute(conn, "DELETE FROM events_fts WHERE event_key=:key", key=key)
                execute(
                    conn,
                    "INSERT INTO events_fts VALUES(:key,:title,:summary)",
                    key=key,
                    title=record.title,
                    summary=record.summary,
                )
        else:
            execute(
                conn,
                "UPDATE events SET version=:v,updated_at=:now WHERE event_key=:key",
                v=ev_version,
                now=now,
                key=key,
            )
        execute(conn, "INSERT INTO event_documents VALUES(:key,:doc)", key=key, doc=doc)
        for i, f in enumerate(record.facts):
            execute(
                conn,
                "INSERT INTO facts VALUES(:doc,:i,:key,:metric,:value,:unit,:period,:vintage)",
                doc=doc,
                i=i,
                key=key,
                metric=f.metric,
                value=None if f.value is None else str(f.value),
                unit=f.unit,
                period=f.period,
                vintage=f.vintage or record.published_at or now,
            )
        urls = list(execute(conn, EVIDENCE_URLS_SQL, key=key).scalars())
        secondary = [
            *record.metadata.get("comparison_evidence", []),
            *record.metadata.get("series_urls", {}).values(),
        ]
        urls.extend(url for url in secondary if url.startswith("https://") and url not in urls)
        line, score, status = (
            (record.summary, record.importance, record.status)
            if current
            else (old["summary"], old["importance"], old["status"])
        )
        assets = set(record.assets)
        assets.update(execute(conn, "SELECT asset_id FROM signals WHERE event_key=:key", key=key).scalars())
        if assets - {"market"}:
            assets.discard("market")
            execute(conn, "DELETE FROM signals WHERE event_key=:key AND asset_id='market'", key=key)
        for asset in sorted(assets or {"market"}):
            execute(
                conn,
                """INSERT INTO signals VALUES(:key,:asset,:line,:imp,NULL,NULL,:horizon,:urls,:state,:v)
                ON CONFLICT(event_key,asset_id) DO UPDATE SET one_liner=excluded.one_liner,importance=excluded.importance,
                direction=NULL,confidence=NULL,horizon=excluded.horizon,evidence_urls=excluded.evidence_urls,state=excluded.state,
                version=excluded.version""",
                key=key,
                asset=asset,
                line=line,
                imp=score,
                horizon="months" if record.module == "research" else "days",
                urls=encode(urls),
                state=(
                    "analysis_pending"
                    if score >= (0.62 if record.module == "news" else 0.65)
                    and status not in {"scheduled", "estimated", "confirmed", "delayed"}
                    else "factual"
                ),
                v=ev_version,
            )
        return True
