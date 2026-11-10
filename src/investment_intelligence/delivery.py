"""Durable reports and delivery claims with stable notification identities."""

from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import getaddresses
import html
import json
import os
import re
import smtplib
import ssl
import tempfile
import unicodedata
from urllib.parse import quote, urlsplit

import requests

from .models import digest, utc
from .storage import EVIDENCE_URLS_SQL, execute


def mail_environment(env):
    """Expand the Gmail shortcut without sharing its password with a custom relay."""
    env = dict(env)
    scope = env.get("EMAIL_SCOPE") or "all"
    if scope not in {"all", "alerts"}:
        raise ValueError("EMAIL_SCOPE must be all or alerts")
    env["EMAIL_SCOPE"] = scope
    transport = env.get("EMAIL_TRANSPORT") or "smtp"
    if transport not in {"smtp", "github"}:
        raise ValueError("EMAIL_TRANSPORT must be smtp or github")
    if transport == "github":
        return env
    address = (env.get("GMAIL_ADDRESS") or "").strip()
    if not env.get("SMTP_HOST") and address:
        env.update(
            SMTP_HOST="smtp.gmail.com",
            SMTP_PORT="587",
            SMTP_SECURITY="starttls",
            SMTP_USER=address,
            SMTP_PASSWORD=(env.get("GMAIL_APP_PASSWORD") or "").replace(" ", ""),
            SMTP_FROM=address,
            SMTP_TO=env.get("SMTP_TO") or address,
        )
    return env


def mail_selected(env, report_id):
    if env.get("EMAIL_TRANSPORT") == "github":
        return report_id.startswith("alert-")
    return env.get("EMAIL_SCOPE") != "alerts" or report_id.startswith("alert-")


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
            doc = db.all("SELECT payload FROM documents WHERE document_id=:d", d=e["current_document"])[0]
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
            if "alert_urls" in e:
                urls = list(e["alert_urls"])
            else:
                urls = [d["canonical_url"] for d in db.all(EVIDENCE_URLS_SQL, key=e["event_key"])]
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
    if not provisional and db.all("SELECT 1 FROM reports WHERE report_id=:r", r=report_id):
        return report_id
    body = render(db, events, title, now, settings, note)
    configured = settings.env.get("EMAIL_TRANSPORT") == "github" or settings.env.get("SMTP_HOST")
    ready = configured and mail_selected(settings.env, report_id) and not settings.demo and not provisional
    with db.write() as conn:
        execute(
            conn,
            """INSERT INTO reports(report_id,filename,body,created_at,mail_state)
               VALUES(:r,:f,:b,:now,:state)
               ON CONFLICT (report_id) DO UPDATE SET body=excluded.body, created_at=excluded.created_at
               WHERE :provisional=1""",
            r=report_id,
            f=filename,
            b=body,
            now=utc(now),
            state="ready" if ready else "disabled",
            provisional=int(provisional),
        )
    return report_id


def mail_message(env, row):
    if env.get("SMTP_USER") and not env.get("SMTP_PASSWORD"):
        raise ValueError("Set the SMTP password or Gmail app password")
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


class IssueCreationRefused(RuntimeError):
    """GitHub explicitly rejected issue creation, so a later attempt is safe."""


def github_request(env, method, path, **kwargs):
    headers = {
        "Authorization": "Bearer " + env["NOTIFICATION_GITHUB_TOKEN"],
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        response = requests.request(
            method,
            "https://api.github.com" + path,
            headers=headers,
            timeout=(5, 20),
            allow_redirects=False,
            **kwargs,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"GitHub notification request failed ({type(exc).__name__})") from None
    if method == "POST" and response.status_code in {400, 401, 403, 404, 410, 422, 429}:
        raise IssueCreationRefused(f"GitHub refused the notification issue (HTTP {response.status_code})")
    if response.status_code != (201 if method == "POST" else 200):
        raise RuntimeError(f"GitHub notification request returned HTTP {response.status_code}")
    return response.json()


def github_notification(env, row):
    """Verify repository identity and look up the stable report marker before any POST."""
    repo = env.get("GITHUB_REPOSITORY") or ""
    owner = env.get("GITHUB_REPOSITORY_OWNER") or ""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}", row["report_id"]):
        raise ValueError("Invalid notification report ID")
    if not (
        env.get("GITHUB_ACTIONS") == "true"
        and env.get("GITHUB_SERVER_URL") == "https://github.com"
        and env.get("NOTIFICATION_GITHUB_TOKEN")
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", owner)
        and re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", repo)
        and repo.split("/")[0].lower() == owner.lower()
    ):
        raise ValueError(
            "GitHub notifications require a GitHub Actions repository, owner and notification token"
        )
    path = "/repos/" + repo
    info = github_request(env, "GET", path)
    identity = info.get("owner", {})
    if not (
        info.get("private") is True
        and info.get("has_issues") is True
        and identity.get("type") == "User"
        and identity.get("login", "").lower() == owner.lower()
        and info.get("full_name", "").lower() == repo.lower()
    ):
        raise ValueError("GitHub notifications require a private personal repository with Issues enabled")

    token = "II" + digest(row["report_id"])[:32].upper()
    marker = f"<!-- investment-intelligence {token} -->"
    # Read issues directly. A newly accepted issue may not be in GitHub's search index yet.
    params = {"state": "all", "creator": "github-actions[bot]", "per_page": 100, "page": 1}
    if row.get("created_at"):
        # GitHub returns UTC timestamps at second precision.
        created = datetime.fromisoformat(row["created_at"]).replace(microsecond=0)
        params["since"] = (created - timedelta(minutes=1)).isoformat()
    while True:
        issues = github_request(env, "GET", path + "/issues", params=params)
        for issue in issues:
            if (
                issue["user"]["login"] == "github-actions[bot]"
                and issue["title"].endswith(f"[{token}]")
                and marker in (issue["body"] or "")
            ):
                return path, None
        if len(issues) < params["per_page"]:
            break
        params["page"] += 1

    # A single verified account receives a mention. Keep publisher text in an indented code
    # block so embedded Markdown, images, issue references and HTML cannot affect the issue.
    source_links = []
    for url in re.findall(r"\[Source\]\((https?://[^\s<>()\\]+)\)", row["body"]):
        if len(url) > 1500:
            continue
        try:
            parts = urlsplit(url)
            if not parts.hostname or parts.username is not None or parts.password is not None:
                continue
        except ValueError:
            continue
        link = "<" + quote(url, safe=":/?&=%+.,_~-") + ">"
        if link not in source_links:
            source_links.append(link)
        if len(source_links) == 6:
            break
    clean = html.unescape(row["body"][:14_000])
    clean = "".join(c for c in clean if c == "\n" or not unicodedata.category(c).startswith("C"))
    lines = clean.replace("@", "＠").splitlines()
    heading = (
        " ".join(re.sub(r"[#*\x60~_<>\\\[\]|!]", " ", lines[0]).split()) if lines else "Investment update"
    )
    text = "\n".join("    " + line for line in "\n".join(lines[1:]).strip().splitlines())[:8_000]
    links = "\n\n" + "\n".join(source_links) if source_links else ""
    payload = {
        "title": f"{heading[:175]} [{token}]",
        "body": f"@{owner}\n\n{text}{links}\n\n    Report {row['report_id']}\n\n{marker}",
    }
    return path, payload


def post_github_notification(env, prepared):
    path, issue = prepared
    if issue is not None:
        github_request(env, "POST", path + "/issues", json=issue)


def send_test_email(env):
    if env.get("EMAIL_TRANSPORT") == "github":
        run_id = env.get("GITHUB_RUN_ID") or ""
        if not run_id.isdecimal():
            raise ValueError("GitHub notification test requires GITHUB_RUN_ID")
        row = {
            "report_id": "test-email-" + run_id,
            "body": "# Investment Intelligence · notification test\n\nYour investment alert notification is connected.",
        }
        prepared = github_notification(env, row)
        post_github_notification(env, prepared)
        return (
            "GitHub confirmed the test notification issue. Check the repository owner's notification inbox."
        )
    if not env.get("SMTP_HOST"):
        raise ValueError("Configure GMAIL_ADDRESS and GMAIL_APP_PASSWORD, or an SMTP relay")
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    row = {
        "report_id": "test-email-" + stamp,
        "body": f"# Investment Intelligence · email test\n\nThe deployment sent this test at {stamp}.",
    }
    msg = mail_message(env, row)
    try:
        smtp(env, msg)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(f"Email test failed: {type(exc).__name__}") from None
    return "The SMTP relay accepted the test email. Check the configured inbox."


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
    if not mail_selected(settings.env, report_id):
        with db.write() as conn:
            execute(
                conn,
                "UPDATE reports SET mail_state='disabled' WHERE report_id=:r AND mail_state='ready'",
                r=report_id,
            )
        return str(target), "disabled"
    github = settings.env.get("EMAIL_TRANSPORT") == "github"
    try:
        msg = github_notification(settings.env, row) if github else mail_message(settings.env, row)
    except (KeyError, TypeError, ValueError, RuntimeError) as exc:
        # Nothing has been transmitted. Preserve a retryable outbox entry for repaired settings.
        with db.write() as conn:
            execute(
                conn,
                "UPDATE reports SET mail_error=:e WHERE report_id=:r AND mail_state='ready'",
                e=(
                    str(exc)
                    if github and isinstance(exc, RuntimeError)
                    else f"{'GitHub' if github else 'SMTP'} configuration: {type(exc).__name__}"
                ),
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
    from .cloud_state import before_external

    try:
        before_external(db, settings.env)
    except BaseException:
        # No notification has been sent. An exit checkpoint must keep this report retryable.
        with db.write() as conn:
            execute(
                conn,
                "UPDATE reports SET mail_state='ready' WHERE report_id=:r AND mail_state='sending'",
                r=report_id,
            )
        raise
    try:
        if github:
            post_github_notification(settings.env, msg)
        else:
            send(settings.env, msg)
        state, err = "sent", None
    except IssueCreationRefused as exc:
        state, err = "ready", str(exc)
    except Exception as exc:
        # A provider can accept a send then drop the connection. Retain the ambiguous claim.
        state, err = "uncertain", type(exc).__name__
    with db.write() as conn:
        execute(
            conn,
            "UPDATE reports SET mail_state=:s,mail_error=:e WHERE report_id=:r",
            s=state,
            e=err,
            r=report_id,
        )
    return str(target), "configuration_error" if state == "ready" else state
