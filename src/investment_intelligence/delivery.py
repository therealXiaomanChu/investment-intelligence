"""Durable reports and conservative SMTP outbox with stable delivery identities."""

from datetime import datetime
from email.message import EmailMessage
from email.utils import getaddresses
import html
import json
import os
import re
import smtplib
import ssl
import tempfile
from urllib.parse import quote

from .models import digest, utc
from .storage import execute

NAMES = {
    "corporate": "Corporate & institutional",
    "research": "Academic research",
    "macro": "US macro",
    "news": "AI, technology & crypto",
}


def plain(value):
    # Markdown must not turn publisher-supplied text into executable HTML or remote images.
    value = html.escape(" ".join(str(value).split()), quote=False)
    return re.sub(r"([\\`*_\[\]<>()#!|])", r"\\\1", value)


def evidence(url):
    return "[Source](" + quote(url, safe=":/?&=%+@.,_-~") + ")"


def render(db, events, title, now, settings, note=""):
    lines = ["# " + plain(title), "", "As of " + now.astimezone(settings.tz).isoformat(timespec="minutes")]
    if settings.demo:
        lines += [
            "",
            "**Offline demonstration using synthetic records. Figures and announcements are illustrative.**",
        ]
    if note:
        lines += ["", note]
    for module, name in NAMES.items():
        section = [e for e in events if e["module"] == module]
        if not section:
            continue
        lines += ["", "## " + name]
        for e in section:
            doc = db.all(
                "SELECT payload,canonical_url FROM documents WHERE document_id=:d", d=e["current_document"]
            )[0]
            signals = db.all("SELECT * FROM signals WHERE event_key=:k ORDER BY asset_id", k=e["event_key"])
            if "fallback_signals" in e and (not signals or signals[0]["version"] != e["version"]):
                signals = e["fallback_signals"]
            payload = json.loads(doc["payload"])
            facts = payload["facts"]
            lines += [
                "",
                "### " + plain(e["title"]),
                "",
                plain(e["summary"]),
                "",
                f"{plain(e['status'])} · {plain(e['event_at'][:10])} · importance {e['importance']:.2f} · event version {e['version']}",
            ]
            if e["scheduled_at"]:
                label = "Check time" if payload["metadata"].get("date_precision") == "day" else "Scheduled"
                lines.append(
                    f"{label} {datetime.fromisoformat(e['scheduled_at']).astimezone(settings.tz).isoformat(timespec='minutes')}"
                )
            if signals:
                lines.append("Assets " + ", ".join(plain(s["asset_id"]) for s in signals))
                if any(s["state"] == "analysis_pending" for s in signals):
                    lines.append("Model analysis pending. Factual summary available.")
                for s in signals:
                    if s["state"] == "analyzed":
                        lines += [
                            "",
                            f"**{plain(s['asset_id'])}** {plain(s['one_liner'])} "
                            f"({plain(s['horizon'])}, direction {s['direction']}, confidence {s['confidence']}).",
                        ]
            if facts:
                lines += ["", "| Metric | Value | Unit | Period |", "|---|---:|---|---|"]
                for f in facts[:18]:
                    val = "unavailable" if f["value"] is None else f["value"]
                    lines.append(
                        "| " + " | ".join(plain(x) for x in (f["metric"], val, f["unit"], f["period"])) + " |"
                    )
                if len(facts) > 18:
                    lines += ["", f"{len(facts)-18} additional fields in the archived observation."]
            urls = [
                d["canonical_url"]
                for d in db.all(
                    """SELECT DISTINCT d.canonical_url FROM documents d
                JOIN event_documents ed ON d.document_id=ed.document_id WHERE ed.event_key=:key ORDER BY d.canonical_url""",
                    key=e["event_key"],
                )
            ]
            urls = e.get("alert_urls", urls)
            urls.extend(payload["metadata"].get("comparison_evidence", []))
            urls.extend(payload["metadata"].get("series_urls", {}).values())
            if payload["metadata"].get("openalex", {}).get("url"):
                urls.append(payload["metadata"]["openalex"]["url"])
            lines += [
                "",
                " · ".join(evidence(u) for u in dict.fromkeys(urls) if u.startswith(("https://", "http://"))),
            ]
    if not events:
        lines += ["", "No qualifying records were available at this cutoff."]
    return "\n".join(lines) + "\n"


def snapshot(db, settings, report_id, filename, events, title, now, note="", provisional=False):
    existing = db.all("SELECT report_id FROM reports WHERE report_id=:r", r=report_id)
    if not existing or provisional:
        body = render(db, events, title, now, settings, note)
        state = (
            "ready" if settings.env.get("SMTP_HOST") and not settings.demo and not provisional else "disabled"
        )
        with db.write() as conn:
            execute(
                conn,
                """INSERT INTO reports(report_id,filename,body,created_at,mail_state)
                 VALUES(:r,:f,:b,:now,:state) ON CONFLICT DO NOTHING""",
                r=report_id,
                f=filename,
                b=body,
                now=utc(now),
                state=state,
            )
            if provisional and existing:
                execute(
                    conn,
                    "UPDATE reports SET body=:body,created_at=:now WHERE report_id=:id",
                    id=report_id,
                    body=body,
                    now=utc(now),
                )
    return report_id


def mail_message(env, row):
    sender = env["SMTP_FROM"]
    recipients = [addr for _, addr in getaddresses([env["SMTP_TO"]]) if addr]
    if not sender or not recipients:
        raise ValueError("Missing SMTP sender or recipients")
    msg = EmailMessage()
    msg["From"], msg["To"] = sender, ", ".join(recipients)
    msg["Message-ID"] = f"<{digest(row['report_id'])}@investment-intelligence.local>"
    msg["Subject"] = row["body"].splitlines()[0].lstrip("# ")
    msg.set_content(row["body"])
    return msg


def smtp(env, msg):
    mode = env.get("SMTP_SECURITY", "starttls")
    if mode not in {"starttls", "ssl"}:
        raise ValueError("SMTP_SECURITY requires starttls or ssl")
    context = ssl.create_default_context()
    port = int(env.get("SMTP_PORT") or (465 if mode == "ssl" else 587))
    if mode == "ssl":
        client = smtplib.SMTP_SSL(env["SMTP_HOST"], port, timeout=20, context=context)
    else:
        client = smtplib.SMTP(env["SMTP_HOST"], port, timeout=20)
    with client:
        if mode == "starttls":
            client.starttls(context=context)
        if env.get("SMTP_USER"):
            client.login(env["SMTP_USER"], env["SMTP_PASSWORD"])
        refused = client.send_message(msg)
        if refused:
            raise RuntimeError("One or more recipients refused")


def deliver(db, settings, report_id, send=smtp):
    row = db.all("SELECT * FROM reports WHERE report_id=:r", r=report_id)[0]
    directory = settings.output
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / row["filename"]
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as tmp:
        tmp.write(row["body"])
    os.replace(tmp.name, target)
    with db.write() as conn:
        execute(
            conn,
            "INSERT INTO file_deliveries VALUES(:id,:time) ON CONFLICT DO NOTHING",
            id=report_id,
            time=utc(datetime.now().astimezone()),
        )
    if row["mail_state"] != "ready" or settings.demo:
        return str(target), row["mail_state"]
    try:
        msg = mail_message(settings.env, row)
    except (KeyError, TypeError, ValueError) as exc:
        # Nothing has been transmitted. Preserve a retryable outbox entry for repaired settings.
        with db.write() as conn:
            execute(
                conn,
                "UPDATE reports SET mail_error=:e WHERE report_id=:r AND mail_state='ready'",
                e=f"SMTP configuration: {type(exc).__name__}",
                r=report_id,
            )
        return str(target), "configuration_error"
    with db.write() as conn:
        changed = execute(
            conn,
            "UPDATE reports SET mail_state='sending' WHERE report_id=:r AND mail_state='ready'",
            r=report_id,
        ).rowcount
    if not changed:
        return str(target), "claimed"
    try:
        send(settings.env, msg)
        state, err = "sent", None
    except Exception as exc:
        # SMTP may accept DATA and then drop the connection. Human inspection prevents duplicates.
        state, err = "uncertain", type(exc).__name__
    with db.write() as conn:
        execute(
            conn,
            "UPDATE reports SET mail_state=:s,mail_error=:e WHERE report_id=:r",
            s=state,
            e=err,
            r=report_id,
        )
    return str(target), state
