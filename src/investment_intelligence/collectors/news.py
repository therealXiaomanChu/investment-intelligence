"""Official publisher announcements and protocol/client release metadata."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup
from defusedxml.ElementTree import fromstring

from ..http import SourceError
from ..models import Fact, Record

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "news"
NAMES = {
    "openai.com": "openai",
    "blogs.nvidia.com": "nvidia",
    "blog.google": "google",
    "www.anthropic.com": "anthropic",
    "bitcoincore.org": "bitcoin",
    "blog.ethereum.org": "ethereum",
    "api.github.com": "agave",
}
NETWORK = r"(mainnet|hoodi|holesky|holešky|sepolia|goerli|ropsten|rinkeby)"
MONTH = (
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2},\s+\d{4}"
)
UTC_TIME = r"\d{1,2}:\d{2}(?::\d{2})?\s*UTC"


@dataclass
class Item:
    external_id: str
    url: str
    title: str
    description: str
    published: str
    updated: str
    precision: str = "instant"
    prerelease: bool = False


@dataclass
class BitcoinAdvisory(Item):
    publisher_severity: str | None = None


def _link(value):
    p = urlparse(value.strip())
    if p.scheme not in {"https", "http"} or not p.hostname or p.username or p.password:
        raise ValueError("Invalid publisher evidence URL")
    clean = [
        (k, v)
        for k, v in parse_qsl(p.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}
    ]
    return urlunparse((p.scheme, p.netloc.lower(), p.path, "", urlencode(clean), ""))


def _plain(value):
    if "<" in value and ">" in value:
        soup = BeautifulSoup(value, "html.parser")
        for node in soup(["script", "style"]):
            node.decompose()
        value = soup.get_text(" ", strip=True)
    return " ".join(value.split())


def _utc(value):
    value = value.strip()
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        stamp = parsedate_to_datetime(value)
    if stamp.tzinfo is None:
        raise ValueError("Feed timestamp missing timezone")
    return stamp.astimezone(timezone.utc).isoformat()


def _bitcoin_advisory(item):
    link = urlparse(item.url)
    return (
        link.scheme == "https"
        and link.netloc == "bitcoincore.org"
        and re.fullmatch(r"/en/\d{4}/\d{2}/\d{2}/[a-zA-Z0-9_-]+/?", link.path)
        and ("/disclose-" in link.path or re.match(r"^CVE-\d{4}-\d+\b", item.title, re.I))
    )


def _bitcoin_severity(content):
    # The publisher states its rating in a standalone paragraph. A quoted advisory
    # or a mention of a high-severity impact in the surrounding prose is not a rating.
    levels = set()
    paragraphs = content.find_all("p") if content.find() is not None else [content]
    for paragraph in paragraphs:
        if paragraph.find_parent(["blockquote", "aside", "li", "pre"]):
            continue
        sentence = " ".join(paragraph.get_text(" ", strip=True).split())
        rating = re.fullmatch(
            r"This (?:issue|vulnerability) is considered (Low|Medium|High|Critical) severity"
            r"(?: and easily discoverable)?\s*\.",
            sentence,
            re.I,
        )
        if rating:
            levels.add(rating[1].lower())
    return levels.pop() if len(levels) == 1 else None


def _rss(raw, *, bitcoin_core=False):
    root = fromstring(raw)
    channel = root.find("channel")
    if root.tag != "rss" or channel is None:
        raise ValueError("Expected RSS channel; publisher response unavailable")
    nodes = channel.findall("item")
    if not nodes:
        raise ValueError("Empty publisher feed; check source format")
    for node in nodes:
        title, url, stamp = (node.findtext(k) for k in ("title", "link", "pubDate"))
        if not title or not url or not stamp:
            raise ValueError("Publisher RSS item missing title, link, or pubDate")
        published = _utc(stamp)
        updated = node.findtext("{http://purl.org/dc/terms/}modified")
        description = node.findtext("description") or ""
        item = Item(
            node.findtext("guid") or url,
            _link(url),
            _plain(title),
            _plain(description),
            published,
            _utc(updated) if updated else published,
        )
        if bitcoin_core and _bitcoin_advisory(item):
            rating = _bitcoin_severity(BeautifulSoup(description, "html.parser"))
            item = BitcoinAdvisory(**asdict(item), publisher_severity=rating)
        yield item


def _agave(raw):
    releases = json.loads(raw)
    if not isinstance(releases, list):
        raise ValueError("GitHub releases list unavailable")
    for row in releases:
        if row.get("draft"):
            continue
        tag = row["tag_name"]
        url = _link(row["html_url"])
        if not url.startswith("https://github.com/anza-xyz/agave/releases/tag/"):
            raise ValueError("Agave release returned unexpected repository")
        yield Item(
            str(row["id"]),
            url,
            f"Agave {tag}",
            row.get("body") or "",
            _utc(row["published_at"]),
            _utc(row["updated_at"]),
            prerelease=row["prerelease"],
        )


def _anthropic(raw, source_url):
    soup = BeautifulSoup(raw, "html.parser")
    found = {}
    for card in soup.select("article, li"):
        heading = card.select_one("a[href] h3, a[href] h2, h3 a[href], h2 a[href]")
        stamp = card.find("time")
        if not heading or not stamp:
            continue
        link = heading if heading.name == "a" else heading.find_parent("a")
        url = _link(urljoin(source_url, link["href"]))
        if urlparse(url).hostname != urlparse(source_url).hostname:
            continue
        date = stamp.get("datetime") or stamp.get_text(" ", strip=True)
        # The listing often supplies a day only. Midnight is a sorting bucket;
        # published_at stays null rather than suggesting a known clock time.
        try:
            when = datetime.strptime(date, "%b %d, %Y").replace(tzinfo=timezone.utc).isoformat()
            precision = "day"
        except ValueError:
            if re.fullmatch(r"\d{4}-\d\d-\d\d", date):
                when = datetime.fromisoformat(date).replace(tzinfo=timezone.utc).isoformat()
                precision = "day"
            else:
                when = _utc(date)
                precision = "instant"
        text = heading.get_text(" ", strip=True)
        if text:
            found[url] = Item(url, url, text, "", when, when, precision=precision)
    if not found:
        raise ValueError("No dated announcements on Anthropic's listing")
    return list(found.values())


def _slug(value):
    return "-".join(re.findall(r"\w+", unicodedata.normalize("NFKC", value).casefold()))


def _initial_key(item, network):
    # Pool long news headlines on the same UTC date. Short headlines and protocol
    # announcements use the canonical URL, keeping each network in its own namespace.
    if not network:
        tokens = re.findall(r"\w+", unicodedata.normalize("NFKC", item.title).casefold())
        words = [t for t in tokens if t not in {"the", "a", "an", "and", "to", "of", "for"}]
        if len(set(words)) >= 5:
            signature = " ".join(sorted(words))
            digest = hashlib.sha256(signature.encode()).hexdigest()[:28]
            return f"news:title:{item.published[:10]}:{digest}"
    namespace = f"protocol:{network}" if network else "news:url"
    return namespace + ":" + hashlib.sha256(item.url.encode()).hexdigest()[:28]


def _upgrade(item):
    # Only a named fork AND a network in the heading identifies the same event
    # across articles. A single fork can activate separately on many testnets.
    match = re.match(
        r"^([\w-]+)(?:\s+upgrade)?\s+(?:on\s+)?(?:ethereum\s+)?" + NETWORK + r"\b", item.title, re.I
    )
    if not match or not re.search(
        r"announcement|upgrade|activation|postponed|delayed|activated", item.title, re.I
    ):
        return None
    subject, network = match.groups()
    content = item.title + ". " + item.description
    postponed = re.search(r"\b(has been postponed|has been delayed|is postponed|is delayed)\b", content, re.I)
    if postponed or re.search(r"\b(postponed|delayed)\b", item.title, re.I):
        return subject, network, "postponed", None, None
    if re.search(r"\b(has been cancel(?:ed|led)|is cancel(?:ed|led))\b", content, re.I):
        return subject, network, "canceled", None, None
    if re.search(r"\b(has activated|activated successfully|is live)\b", content, re.I):
        return subject, network, "published", None, None
    if re.search(
        r"\b(?:not (?:yet )?(?:scheduled|confirmed)|no (?:activation )?date|previously scheduled)\b",
        content,
        re.I,
    ):
        return None
    schedule = re.search(r"\b(scheduled|expected|estimated|planned|will activate)\b(.{0,220})", content, re.I)
    if not schedule:
        return None
    # Accept an explicit UTC instant in proximity to the scheduling phrase.
    context = schedule[2]
    iso = re.search(r"\b(\d{4}-\d\d-\d\d[T ]\d\d:\d\d(?::\d\d)?(?:Z|\sUTC))\b", context)
    date = re.search(r"(" + MONTH + r")[,\s]*(?:at\s+)?(" + UTC_TIME + r")", context, re.I)
    at, day = None, None
    if iso:
        at = _utc(iso[1].replace(" UTC", "+00:00"))
    elif date:
        stamp = date[2].upper().replace("UTC", "").strip()
        fmt = "%B %d, %Y %H:%M:%S" if stamp.count(":") == 2 else "%B %d, %Y %H:%M"
        at = datetime.strptime(date[1] + " " + stamp, fmt).replace(tzinfo=timezone.utc).isoformat()
    else:
        day_match = re.search(MONTH, context, re.I)
        day = day_match[0] if day_match else None
    if at is None and day is None:
        return None
    estimated = schedule[1].lower() in {"expected", "estimated", "planned"}
    return subject, network, "estimated" if estimated else "scheduled", at, day


def _record(ctx, source, policy, item, previous):
    title = item.title
    network = policy.get("network")
    symbols = {symbol.upper() for symbol in policy["symbols"]}
    text = (title + " " + item.description[:600]).casefold()
    for asset in ctx.watchlist["assets"]:
        names = [asset["symbol"], asset.get("name") or "", *asset.get("aliases", [])]
        if any(
            len(n) > 2 and re.search(r"(?<!\w)" + re.escape(n.casefold()) + r"(?!\w)", text) for n in names
        ):
            symbols.add(asset["symbol"].upper())
    assets = tuple(a["id"] for a in ctx.watchlist["assets"] if a["symbol"].upper() in symbols)
    if not assets:
        return None
    key = previous.get("key") or ""
    metadata = {
        "publisher": policy["name"],
        "published_precision": item.precision,
        "source_updated_at": item.updated,
        "digest": hashlib.sha256(item.description.encode()).hexdigest(),
    }
    facts = []
    at, status, kind, score = None, "published", "news", 0.64
    summary = f"{policy['name']} published “{title}”."
    if item.precision == "day":
        facts.append(Fact("publication_day", item.published[:10]))
    if network:
        kind, score = "protocol_news", 0.74
        if policy["format"] == "github_releases":
            kind, score = "client_release", 0.77
            channel = "pre-release" if item.prerelease else "release"
            facts.append(Fact("release_channel", channel))
            summary = f"Anza published {title} as a client {channel}."
        if isinstance(item, BitcoinAdvisory):
            severity = item.publisher_severity
            if severity:
                facts.append(Fact("publisher_severity", severity))
                summary = f"Bitcoin Core rates this security advisory {severity}."
                if severity in {"high", "critical"}:
                    score = 0.9
        change = _upgrade(item) if network == "ETH" else None
        if change:
            fork, target, status, at, day = change
            key = f"upgrade:ETH:{_slug(fork)}:{_slug(target)}"
            kind, score = "protocol_upgrade", 0.90
            metadata["check_source"] = source["id"]
            facts.extend([Fact("network", target), Fact("upgrade_status", status)])
            if at or day:
                facts.append(Fact("announced_activation", at or day))
            timing = f" for {at or day}" if (at or day) else ""
            summary = f"{policy['name']} reports the {fork} {target} upgrade as {status}{timing}."
        elif key.startswith("upgrade:ETH:"):
            # An edited announcement can remove an old schedule without reporting
            # activation. Keep its identity and never infer a completed upgrade.
            kind, status, score = "protocol_upgrade", "unavailable", 0.85
            summary = f"{policy['name']} updated “{title}”. Activation status is unavailable."
    elif re.search(r"\b(S-1|initial public offering|IPO)\b", title, re.I):
        kind, score = "ipo_announcement", 0.86
    elif re.search(
        r"\b(launch(?:es|ed)?|introduc(?:es|ing)|releas(?:es|ed)|pricing|partner(?:s|ship)|benchmark|funding)\b",
        title,
        re.I,
    ):
        score = 0.72
    return Record(
        source_id=source["id"],
        external_id=item.external_id,
        url=item.url,
        module="news",
        kind=kind,
        event_key=key or _initial_key(item, network),
        title=title,
        summary=summary,
        event_at=at or item.updated,
        published_at=item.published if item.precision == "instant" else None,
        status=status,
        assets=assets,
        facts=tuple(facts),
        importance=score,
        scheduled_at=at,
        metadata=metadata,
    )


def collect(ctx, source):
    config = ctx.config("news")
    policy = config["publishers"][source["id"]]
    fmt = policy["format"]
    if fmt == "github_releases":
        items = _agave(ctx.http.get(source["url"], params={"per_page": 100}))
    elif fmt == "anthropic":
        items = _anthropic(ctx.http.get(source["url"]), source["url"])
    else:
        items = _rss(ctx.http.get(source["url"]), bitcoin_core=source["id"] == "bitcoin_core")
    previous = ctx.cursor.get("items", {})
    items = {i.external_id: i for i in items}
    # Restrict bootstrap history to keep an initial news scan timely. Backfill covers
    # retained feed history only. A changed previously-seen document is always eligible.
    days = config["backfill_lookback_days" if ctx.backfill else "initial_lookback_days"]
    earliest = ctx.now - timedelta(days=int(days))
    if previous and ctx.cursor.get("last_scan"):
        earliest = min(earliest, datetime.fromisoformat(ctx.cursor["last_scan"]))
    state = {k: v for k, v in previous.items() if k in items}
    count = 0
    for item in sorted(items.values(), key=lambda i: (i.updated, i.external_id)):
        old = previous.get(item.external_id, {})
        if not old and datetime.fromisoformat(item.updated) < earliest:
            continue
        stamp = hashlib.sha256(json.dumps(asdict(item), sort_keys=True).encode()).hexdigest()
        if isinstance(item, BitcoinAdvisory):
            stamp = "btc-severity-v1:" + stamp
        if stamp == old.get("stamp"):
            continue
        if isinstance(item, BitcoinAdvisory) and not item.publisher_severity:
            # A short RSS description may omit the rating. Verify only this official
            # advisory page, after applying the lookback, cache and quota checks.
            try:
                page = BeautifulSoup(ctx.http.get(item.url), "html.parser")
                article = page.select_one("article.h-entry .e-content")
                heading = page.select_one("article.h-entry h1")
                if article is None or heading is None or _plain(heading.get_text()) != item.title:
                    raise ValueError("Official advisory page could not be verified")
                item.publisher_severity = _bitcoin_severity(article)
                if not item.publisher_severity:
                    raise ValueError("No unambiguous publisher rating yet")
            except (SourceError, ValueError):
                ctx.warn(f"Bitcoin Core advisory rating unavailable: {item.url}")
                state[item.external_id] = {**old, "retry_severity": True}
                continue
        record = _record(ctx, source, policy, item, old)
        if record is not None:
            yield record
            count += 1
        state[item.external_id] = {"stamp": stamp, "key": record.event_key if record else None}
        if count >= ctx.limit:
            break
    ctx.next_cursor["items"] = state
    # While capped, retain the catch-up floor even if the next scan occurs much later.
    ctx.next_cursor["last_scan"] = (earliest if count >= ctx.limit else ctx.now).isoformat()


def demo_response(url, method, payload, phase):
    parsed = urlparse(url)
    name = NAMES.get(parsed.hostname)
    if not name:
        return None
    if name == "agave" and parsed.path != "/repos/anza-xyz/agave/releases":
        return None
    if name == "anthropic":
        path = "anthropic.html"
    elif name == "agave":
        path = "agave.json"
    else:
        path = f"ethereum-{min(int(phase), 1)}.xml" if name == "ethereum" else name + ".xml"
    return (FIXTURES / path).read_bytes()
