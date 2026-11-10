"""Retrieve the previous week's new/updated arXiv records and exact-DOI metadata."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from defusedxml.ElementTree import fromstring

from ..models import Fact, Record

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"
SEARCH = "{http://a9.com/-/spec/opensearch/1.1/}"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "research"
PAPER_ID = re.compile(r"(?P<id>(?:\d{4}\.\d{4,5}|[a-zA-Z.-]+/\d{7}))(?:v(?P<version>\d+))?$")


def weekly_window(now, tz_name="America/New_York"):
    local = now.astimezone(ZoneInfo(tz_name))
    monday = local.date() - timedelta(days=local.weekday())
    end = datetime.combine(monday, time(), tzinfo=local.tzinfo)
    return (end - timedelta(days=7)).astimezone(timezone.utc), end.astimezone(timezone.utc)


def _text(node, tag):
    return " ".join((node.findtext(tag) or "").split())


def _instant(value):
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.tzinfo is None:
        raise ValueError("arXiv item has no publication timezone")
    return instant.astimezone(timezone.utc)


def parse_page(raw):
    root = fromstring(raw)
    if root.tag != ATOM + "feed":
        raise ValueError("arXiv response is not an Atom feed")
    total = root.findtext(SEARCH + "totalResults")
    if total is None:
        raise ValueError("arXiv response missing totalResults")
    papers = []
    for entry in root.findall(ATOM + "entry"):
        identifier = _text(entry, ATOM + "id")
        match = PAPER_ID.fullmatch(urlparse(identifier).path.removeprefix("/abs/"))
        if not match or urlparse(identifier).hostname not in {"arxiv.org", "www.arxiv.org"}:
            raise ValueError("Unrecognized arXiv entry or API error")
        title = _text(entry, ATOM + "title")
        if not title:
            raise ValueError("arXiv entry missing title")
        authors = [_text(node, ATOM + "name") for node in entry.findall(ATOM + "author")]
        doi = _text(entry, ARXIV + "doi").lower().removeprefix("https://doi.org/")
        papers.append(
            {
                "id": match["id"],
                "version": int(match["version"] or 1),
                "title": title,
                "abstract": _text(entry, ATOM + "summary"),
                "updated": _instant(_text(entry, ATOM + "updated")).isoformat(),
                "published": _instant(_text(entry, ATOM + "published")).isoformat(),
                "authors": authors,
                "doi": doi,
                "categories": [node.get("term") for node in entry.findall(ATOM + "category")],
            }
        )
    return papers, int(total)


def _matched(paper, config):
    content = paper["title"] + " " + paper["abstract"]
    return {
        topic: [t for t in terms if re.search(r"(?<!\w)" + re.escape(t) + r"(?!\w)", content, re.I)]
        for topic, terms in config["topics"].items()
    }


def _dois(paper):
    identifiers = [f"10.48550/arxiv.{paper['id'].lower()}"]
    if re.fullmatch(r"10\.\d{4,9}/[^|,\s]+", paper["doi"]):
        identifiers.append(paper["doi"])
    return identifiers


def _metadata(ctx, papers, config):
    by_doi = {}
    keys = sorted({doi for paper in papers for doi in _dois(paper)})
    # The list endpoint accepts up to 100 OR-ed values. Smaller chunks fit most proxies.
    for start in range(0, len(keys), 30):
        params = {
            "filter": "doi:" + "|".join(keys[start : start + 30]),
            "per_page": 30,
            "select": "id,doi,authorships,locations",
        }
        if ctx.env.get("OPENALEX_API_KEY"):
            params["api_key"] = ctx.env["OPENALEX_API_KEY"]
        body = json.loads(ctx.http.get(config["openalex_url"], params=params))
        if not isinstance(body.get("results"), list):
            raise ValueError("OpenAlex works response missing results")
        for work in body["results"]:
            candidates = {(work.get("doi") or "").lower().removeprefix("https://doi.org/")}
            # Merged conference and preprint works can return a publisher DOI
            # even when filtering by arXiv DOI. Accept only explicit location IDs.
            for location in work.get("locations") or []:
                landing = urlparse(location.get("landing_page_url") or "")
                if landing.hostname in {"doi.org", "dx.doi.org"}:
                    candidates.add(landing.path.lstrip("/").lower())
                elif landing.hostname == "arxiv.org" and landing.path.startswith("/abs/"):
                    match = PAPER_ID.fullmatch(landing.path.removeprefix("/abs/"))
                    if match:
                        candidates.add("10.48550/arxiv." + match["id"].lower())
            matches = candidates.intersection(keys)
            if not matches or not re.fullmatch(r"https://openalex\.org/W\d+", work.get("id") or ""):
                continue
            authors = []
            for authorship in work["authorships"]:
                author = authorship.get("author") or {}
                authors.append(
                    {
                        "id": author.get("id"),
                        "name": author.get("display_name"),
                        "orcid": author.get("orcid"),
                        "institutions": [
                            {"id": org.get("id"), "name": org.get("display_name")}
                            for org in authorship.get("institutions") or []
                        ],
                    }
                )
            for doi in matches:
                by_doi[doi] = {"url": work["id"], "authors": authors}
    return by_doi


def _tracked(people, config):
    author_ids = {str(i).split("/")[-1] for i in config.get("tracked_author_ids", [])}
    org_ids = {str(i).split("/")[-1] for i in config.get("tracked_institution_ids", [])}
    return any(
        (person.get("id") or "").split("/")[-1] in author_ids
        or any((org.get("id") or "").split("/")[-1] in org_ids for org in person["institutions"])
        for person in people
    )


def _record(ctx, source, paper, match, config, enrichment):
    topics = sorted(topic for topic, terms in match.items() if terms)
    terms = sorted({term for group in match.values() for term in group})
    symbols = {s.upper() for topic in topics for s in config["topic_symbols"].get(topic, [])}
    assets = tuple(a["id"] for a in ctx.watchlist["assets"] if a["symbol"].upper() in symbols)
    people = enrichment.get("authors", [])
    score = min(
        0.96,
        0.52 + 0.07 * len(topics) + 0.025 * min(5, len(terms)) + (0.10 if _tracked(people, config) else 0),
    )
    url = f"https://arxiv.org/abs/{paper['id']}v{paper['version']}"
    summary = f"{', '.join(paper['authors'][:3])} posted v{paper['version']} on {', '.join(topics)}."
    facts = [
        Fact("arxiv_version", str(paper["version"])),
        Fact("technical_tags", ", ".join(terms)),
        Fact("authors", ", ".join(paper["authors"])),
    ]
    if enrichment:
        facts.append(Fact("openalex_work", enrichment["url"]))
        institutions = sorted({org["name"] for p in people for org in p["institutions"] if org["name"]})
        if institutions:
            facts.append(Fact("institutions", ", ".join(institutions)))
    return Record(
        source_id=source["id"],
        external_id=paper["id"],
        url=url,
        module="research",
        kind="paper",
        event_key="arxiv:" + paper["id"],
        title=paper["title"],
        summary=summary,
        event_at=paper["updated"],
        published_at=paper["updated"],
        assets=assets,
        facts=tuple(facts),
        importance=round(score, 4),
        metadata={
            "arxiv_id": paper["id"],
            "version": paper["version"],
            "authors": paper["authors"],
            "first_published_at": paper["published"],
            "categories": paper["categories"],
            "technical_tags": terms,
            "abstract_excerpt": paper["abstract"][:900],
            "abstract_hash": hashlib.sha256(paper["abstract"].encode()).hexdigest(),
            "openalex": enrichment or {"status": "unavailable"},
        },
    )


def collect(ctx, source):
    config = ctx.config("research")
    start, end = weekly_window(ctx.now, ctx.env.get("TIMEZONE", "America/New_York"))
    if ctx.backfill:
        start -= timedelta(weeks=max(0, int(config["backfill_weeks"]) - 1))
    terms = sorted({str(x).replace('"', "") for group in config["topics"].values() for x in group})
    query = " OR ".join(f'abs:"{t}" OR ti:"{t}"' for t in terms)
    mode = "backfill" if ctx.backfill else "weekly"
    prior = ctx.cursor if ctx.cursor.get("mode") == mode else {}
    # Finish an interrupted window before advancing to the latest reporting week.
    if prior.get("incomplete"):
        start, end = _instant(prior["start"]), _instant(prior["end"])
    same_window = prior.get("start") == start.isoformat() and prior.get("end") == end.isoformat()
    seen = dict(prior.get("seen", {})) if same_window else {}
    selected = {}
    page_size = min(100, max(10, ctx.limit))
    stage = prior.get("stage", "new") if prior.get("incomplete") else "new"
    old_offset = int(prior.get("next_offset", 0)) if prior.get("incomplete") else 0
    offset = max(0, old_offset - min(3, page_size // 4))
    # The documented API supports a date filter for submission, but only a sort
    # for revisions. Fetch new papers first so a late run still has useful output.
    first_minute = start.strftime("%Y%m%d%H%M")
    last_minute = (end - timedelta(minutes=1)).strftime("%Y%m%d%H%M")
    new_query = f"({query}) AND submittedDate:[{first_minute} TO {last_minute}]"
    completed = False
    for _ in range(int(config["max_pages_per_run"])):
        data = ctx.http.get(
            source["url"],
            params={
                "search_query": new_query if stage == "new" else query,
                "start": offset,
                "max_results": page_size,
                "sortBy": "submittedDate" if stage == "new" else "lastUpdatedDate",
                "sortOrder": "descending",
            },
        )
        papers, total = parse_page(data)
        if not papers and offset < total:
            raise ValueError("arXiv returned an empty page inside reported result range")
        exhausted = False
        for paper in papers:
            when = _instant(paper["updated"])
            if stage == "revisions" and when < start:
                exhausted = True
                break
            if start <= when < end and paper["id"] not in selected:
                matches = _matched(paper, config)
                stamp = hashlib.sha256(json.dumps(paper, sort_keys=True).encode()).hexdigest()
                if any(matches.values()) and seen.get(paper["id"]) != stamp:
                    selected[paper["id"]] = (paper, matches, stamp)
            offset += 1
            if len(selected) >= ctx.limit:
                break
        if exhausted or offset >= total:
            if stage == "revisions":
                completed = True
                break
            stage, offset = "revisions", 0
        if len(selected) >= ctx.limit:
            break
    try:
        enriched = _metadata(ctx, [entry[0] for entry in selected.values()], config)
        enrichment_ok = True
    except Exception as exc:
        # Never echo a request URL, which may contain an OpenAlex token.
        ctx.warn(f"OpenAlex metadata unavailable ({type(exc).__name__}); arXiv still collected")
        enriched, enrichment_ok = {}, False
    for paper, match, stamp in selected.values():
        work = next((enriched[i] for i in _dois(paper) if i in enriched), {})
        yield _record(ctx, source, paper, match, config, work)
        if enrichment_ok:
            seen[paper["id"]] = stamp
    ctx.next_cursor.update(
        start=start.isoformat(),
        end=end.isoformat(),
        seen=seen,
        mode=mode,
        stage=stage,
        incomplete=not completed,
        next_offset=0 if completed else offset,
    )
    if not completed:
        ctx.warn("arXiv weekly scan incomplete at the fetch cap; rerun paper_weekly to continue")


def demo_response(url, method, payload, phase):
    parsed = urlparse(url)
    if parsed.hostname == "export.arxiv.org":
        data = (FIXTURES / f"papers-{min(int(phase), 1)}.xml").read_bytes()
        # Keep pagination behavior observable with very small user fetch limits.
        import xml.etree.ElementTree as ET

        root = fromstring(data)
        entries = root.findall(ATOM + "entry")
        query = parse_qs(parsed.query)
        offset = int(query.get("start", [0])[0])
        size = int(query.get("max_results", [100])[0])
        for node in entries:
            root.remove(node)
        root.extend(entries[offset : offset + size])
        return ET.tostring(root, encoding="utf-8")
    if parsed.hostname == "api.openalex.org":
        return (FIXTURES / "works.json").read_bytes()
    return None
