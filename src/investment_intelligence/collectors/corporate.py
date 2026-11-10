"""SEC issuer filings, quarterly institutional disclosures and IR calendars."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
import hashlib
import json
import re
from urllib.parse import quote, urlsplit
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from ..models import Fact, Record

NY = ZoneInfo("America/New_York")
UTC = timezone.utc
ITEMS = {
    "1.01": "material agreement",
    "1.05": "material cybersecurity incident",
    "2.01": "acquisition or disposition",
    "2.02": "operating results",
    "2.05": "exit costs",
    "3.02": "unregistered equity sale",
    "4.02": "nonreliance on financial statements",
    "5.02": "director or officer change",
    "7.01": "Regulation FD disclosure",
    "8.01": "other events",
    "9.01": "financial statements and exhibits",
}
BASE = "https://data.sec.gov"
TICKERS = "https://www.sec.gov/files/company_tickers.json"
SPCX_CIK = "0001181412"
SEC_FORMS = {"10-Q", "10-K"}
METRICS = {
    "revenue": (
        "USD",
        ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"],
    ),
    "diluted_eps": ("USD/shares", ["EarningsPerShareDiluted"]),
    "gross_profit": ("USD", ["GrossProfit"]),
    "operating_income": ("USD", ["OperatingIncomeLoss"]),
    "operating_cash_flow": ("USD", ["NetCashProvidedByUsedInOperatingActivities"]),
    "capex": ("USD", ["PaymentsToAcquirePropertyPlantAndEquipment"]),
    "cash": ("USD", ["CashAndCashEquivalentsAtCarryingValue"]),
    "long_term_debt_noncurrent": ("USD", ["LongTermDebtNoncurrent"]),
}


def _safe_xml(body):
    if re.search(rb"<!\s*(DOCTYPE|ENTITY)", body, re.I):
        raise ValueError("Unexpected DTD in source XML")
    return ET.fromstring(body)


def _stamp(value):
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("Source must declare a time zone")
    return stamp.astimezone(UTC).isoformat()


def _at_day(value):
    return datetime.combine(date.fromisoformat(value), time(), NY).astimezone(UTC).isoformat()


def _json(ctx, url, headers):
    return json.loads(ctx.http.get(url, headers=headers), parse_float=Decimal)


def _cik(value):
    value = str(value or "").upper().removeprefix("CIK").lstrip(": -")
    if not re.fullmatch(r"\d{1,10}", value):
        return None
    return value.zfill(10)


def _sec_headers(ctx):
    ua = ctx.env.get("SEC_USER_AGENT", "")
    if ctx.demo:
        return {"User-Agent": "Offline synthetic fixture reader"}
    if not re.search(r"\S+@\S+\.\S+", ua) or len(ua.split()) < 2:
        raise ValueError("SEC_USER_AGENT requires a real organization/name and contact email")
    return {"User-Agent": ua}


def _issuers(ctx, headers=None):
    issuers = defaultdict(list)
    tickers = None
    for asset in ctx.watchlist["assets"]:
        symbol = str(asset.get("symbol", ""))
        cik = _cik(asset.get("cik") or asset.get("issuer_id"))
        if not cik and asset.get("sec_name") and headers:
            if tickers is None:
                tickers = _json(ctx, TICKERS, headers).values()
            matches = [
                t
                for t in tickers
                if t["ticker"] == symbol and t["title"].casefold() == asset["sec_name"].casefold()
            ]
            if len(matches) == 1:
                cik = _cik(matches[0]["cik_str"])
            else:
                ctx.warn(f"No unique SEC issuer mapping for {symbol}")
        if symbol == "SPCX" and cik != SPCX_CIK:
            ctx.warn("Rejected SPCX issuer mapping: pinned SpaceX CIK is 0001181412")
            continue
        if cik:
            issuers[cik].append(asset)
    return issuers


def _asset_ids(assets, filed):
    result = []
    for asset in assets:
        start = str(asset.get("valid_from") or "")
        if asset.get("symbol") == "SPCX":
            start = max(start, "2026-06-12")
        if start <= filed <= str(asset.get("valid_to") or "9999-12-31"):
            result.append(asset["id"])
    return tuple(result)


def _rows(columns):
    keys = ("form", "accessionNumber", "filingDate", "reportDate", "primaryDocument")
    n = len(columns["form"])
    if any(len(columns[k]) != n for k in keys):
        raise ValueError("SEC submissions columns have unequal lengths")
    for i in range(n):
        row = {k: columns[k][i] for k in keys}
        for k in ("acceptanceDateTime", "items", "primaryDocDescription"):
            row[k] = columns[k][i] if k in columns else ""
        if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", row["accessionNumber"]):
            raise ValueError("Malformed SEC accession")
        yield row


def _submissions(ctx, cik, headers, floor=None):
    sub = _json(ctx, f"{BASE}/submissions/CIK{cik}.json", headers)
    if _cik(sub["cik"]) != cik:
        raise ValueError("SEC submissions returned a different issuer")
    filings = sub["filings"]
    rows = list(_rows(filings["recent"]))
    if floor and (ctx.backfill or (rows and min(r["filingDate"] for r in rows) > floor)):
        for file in filings.get("files", []):
            if file["filingTo"] >= floor and re.fullmatch(r"CIK\d+-submissions-\d+\.json", file["name"]):
                rows.extend(_rows(_json(ctx, f"{BASE}/submissions/{file['name']}", headers)))
    return sub, rows


def _folder(cik, row):
    accession = row["accessionNumber"].replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}/"


def _primary_url(cik, row):
    name = row["primaryDocument"]
    if not re.fullmatch(r"[\w.\-/]+", name) or ".." in name or name.startswith("/"):
        raise ValueError("Invalid primary document path")
    return _folder(cik, row) + quote(name)


def _published(row):
    return _stamp(row["acceptanceDateTime"]) if row["acceptanceDateTime"] else None


def _period(entry):
    return "/".join(filter(None, [entry.get("start"), entry["end"]]))


def financial_facts(cfacts, row):
    """Choose accession-matched facts and preserve exact start/end and units."""
    concepts = cfacts["facts"].get("us-gaap", {})
    acc, end = row["accessionNumber"], row["reportDate"]
    facts, selected = [], {}
    for metric, (unit, names) in METRICS.items():
        for name in names:
            entries = [e for e in concepts.get(name, {}).get("units", {}).get(unit, []) if e["accn"] == acc]
            # Distinct values for the same period are ambiguous. Exact duplicates collapse.
            by_period = defaultdict(dict)
            for entry in entries:
                if entry["end"] == end:
                    by_period[_period(entry)][str(entry["val"])] = entry
            if not by_period:
                continue
            for period, candidates in sorted(by_period.items()):
                if len(candidates) != 1:
                    continue
                value, entry = next(iter(candidates.items()))
                facts.append(Fact(metric, value, unit, period, row["filingDate"]))
                selected[metric, period] = Decimal(value)
                if metric != "revenue" or not entry.get("start"):
                    continue
                start = date.fromisoformat(entry["start"])
                finish = date.fromisoformat(end)
                comparable = set()
                for previous in entries:
                    if not previous.get("start"):
                        continue
                    prior_start = date.fromisoformat(previous["start"])
                    if not 357 <= (start - prior_start).days <= 373:
                        continue
                    prior_end = date.fromisoformat(previous["end"])
                    if 357 <= (finish - prior_end).days <= 373 and abs(
                        (finish - start).days - (prior_end - prior_start).days
                    ) <= 7:
                        comparable.add((_period(previous), str(previous["val"])))
                if len(comparable) == 1:
                    prior_period, prior_value = comparable.pop()
                    prior = Decimal(prior_value)
                    facts.append(Fact("prior_year_revenue", str(prior), unit, prior_period, row["filingDate"]))
                    if prior:
                        yoy = (selected[metric, period] / prior - 1) * 100
                        facts.append(Fact("revenue_growth_yoy", str(round(yoy, 3)), "%", period, row["filingDate"]))
            break
    for (metric, period), value in selected.items():
        revenue = selected.get(("revenue", period))
        if metric in {"gross_profit", "operating_income"} and revenue:
            margin = "gross_margin" if metric == "gross_profit" else "operating_margin"
            facts.append(Fact(margin, str(round(value / revenue * 100, 3)), "%", period, row["filingDate"]))
        if metric == "operating_cash_flow" and ("capex", period) in selected:
            facts.append(
                Fact(
                    "free_cash_flow", str(value - selected["capex", period]), "USD", period, row["filingDate"]
                )
            )
    return facts


def _filing(ctx, cik, name, assets, row, headers, cache):
    form, acc, period = row["form"], row["accessionNumber"], row["reportDate"]
    base_form = form.split("/")[0]
    url = _primary_url(cik, row)
    raw = ctx.http.get(url, headers=headers)
    if not re.search(rb"<(?:html|\?xml|html:html|ownershipDocument)\b", raw[:40_000], re.I):
        raise ValueError("Unrecognized primary filing structure")
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style", "ix:hidden", "ix:header"]):
        tag.decompose()
    body = " ".join(soup.stripped_strings)
    if not body or re.search(
        "request rate threshold exceeded|your request originates from", body[:600], re.I
    ):
        raise ValueError("SEC returned an access page")
    facts = [Fact("form", form, "", period, row["filingDate"])]
    fields = {
        "accession": acc,
        "issuer_cik": cik,
        "form": form,
        "report_period": period,
        "primary_sha256": hashlib.sha256(raw).hexdigest(),
        "transcript": "unavailable",
    }
    if base_form in SEC_FORMS:
        if "xbrl" not in cache:
            cache["xbrl"] = _json(ctx, f"{BASE}/api/xbrl/companyfacts/CIK{cik}.json", headers)
        if _cik(cache["xbrl"]["cik"]) != cik:
            raise ValueError("SEC companyfacts returned a different issuer")
        facts += financial_facts(cache["xbrl"], row)
        fields["unavailable"] = sorted(set(METRICS) - {f.metric for f in facts}) + ["guidance"]
    items = sorted(set(re.findall(r"\b\d\.\d{2}\b", row["items"] or "")))
    if base_form == "8-K":
        items = items or sorted(set(re.findall(r"\bItem\s+(\d\.\d{2})\b", body, re.I)))
        facts += [Fact("disclosed_items", ", ".join(items) or "unavailable", "", period, row["filingDate"])]
        fields["items"] = items
    published = _published(row)
    # Periodic amendments are added to the same report, with their own accession and evidence.
    key = f"sec:{cik}:{base_form}:{period}" if base_form in SEC_FORMS and period else f"sec:{cik}:{acc}"
    title = f"{name} filed {form}"
    summary = title + (f" for {period}" if period else "") + "."
    if items:
        summary += (
            " Disclosed items: "
            + ", ".join(f"{code} ({ITEMS[code]})" if code in ITEMS else code for code in items)
            + "."
        )
    revenue = [f for f in facts if f.metric == "revenue"]
    if revenue:
        chosen = sorted(revenue, key=lambda f: f.period)[-1]
        summary += f" Revenue for {chosen.period}: {chosen.value} {chosen.unit}."
    return Record(
        source_id="sec_filings",
        external_id=acc,
        url=url,
        module="corporate",
        kind=base_form,
        event_key=key,
        title=title,
        summary=summary,
        event_at=published or _at_day(row["filingDate"]),
        published_at=published,
        assets=tuple(assets),
        facts=tuple(facts),
        status="revised" if form.endswith("/A") else "published",
        importance=0.86 if base_form in SEC_FORMS | {"8-K", "S-1"} else 0.45,
        metadata=fields,
    )


def _slices(ctx, subjects):
    """Share the source quota across issuers, rotating when the quota is small."""
    if not subjects:
        return
    start = ctx.cursor.get("issuer_offset", 0) % len(subjects)
    selected = (subjects[start:] + subjects[:start])[: ctx.limit]
    if len(selected) < len(subjects):
        ctx.next_cursor["issuer_offset"] = (start + len(selected)) % len(subjects)
    quota, extra = divmod(ctx.limit, len(selected))
    for index, subject in enumerate(selected):
        yield subject, quota + int(index < extra)


def _filings(ctx, conf, headers):
    for (cik, assets), limit in _slices(ctx, list(_issuers(ctx, headers).items())):
        try:
            cache = {}
            state = dict(ctx.cursor.get(cik, {}))
            window = int(conf.get("backfill_days" if ctx.backfill else "lookback_days", 90))
            floor = (ctx.now.date() - timedelta(days=window)).isoformat()
            floor = min(floor, state.get("last_check", floor), state.get("pending_from", floor))
            sub, rows = _submissions(ctx, cik, headers, floor)
            symbols = set(sub.get("tickers", []))
            if symbols:
                assets = [a for a in assets if a["symbol"] in symbols]
                if not assets:
                    raise ValueError("current SEC ticker(s) do not match configured issuer")
            seen = dict(state.get("seen", {}))
            eligible = sorted(
                (
                    r
                    for r in rows
                    if r["form"].split("/")[0] in conf["forms"]
                    and floor <= r["filingDate"] <= ctx.now.date().isoformat()
                    and _asset_ids(assets, r["filingDate"])
                ),
                key=lambda r: (r["filingDate"], r["accessionNumber"]),
            )
            if ctx.backfill:
                # Consecutive manual invocations page through the configured history oldest first.
                previous = state.get("backfill_after", "")
                eligible = [r for r in eligible if r["filingDate"] + r["accessionNumber"] > previous]
            else:
                eligible = [r for r in eligible if r["accessionNumber"] not in seen]
                # Front-load material filings on initial catch-up. Older failures cannot block new filings.
                eligible.sort(key=lambda r: r["form"].split("/")[0] not in SEC_FORMS | {"S-1", "8-K"})
            backlog = eligible[limit:]
            failed = []
            for row in eligible[:limit]:
                acc = row["accessionNumber"]
                try:
                    record = _filing(
                        ctx, cik, sub["name"], _asset_ids(assets, row["filingDate"]), row, headers, cache
                    )
                except (ValueError, KeyError, IndexError, ArithmeticError, OSError, RuntimeError) as exc:
                    failed.append(row)
                    ctx.warn(f"SEC filing {acc} unavailable: {exc}")
                    continue
                yield record
                seen[acc] = row["filingDate"]
            pending = failed + backlog
            if pending:
                state["pending_from"] = min(r["filingDate"] for r in pending)
            else:
                state.pop("pending_from", None)
            if ctx.backfill:
                if backlog and not failed:
                    last = eligible[limit - 1]
                    state["backfill_after"] = last["filingDate"] + last["accessionNumber"]
                elif not failed:
                    state.pop("backfill_after", None)
            state.update(
                seen={a: d for a, d in seen.items() if d >= floor}, last_check=ctx.now.date().isoformat()
            )
            ctx.next_cursor[cik] = state
        except (ValueError, KeyError, IndexError, OSError, RuntimeError) as exc:
            ctx.warn(f"SEC issuer {cik} unavailable: {exc}")


def _xml_block(document):
    match = re.search(rb"<XML>\s*(.*?)\s*</XML>", document, re.I | re.S)
    if not match:
        raise ValueError("Missing XML in 13F submission")
    return _safe_xml(match[1].strip())


def parse_holdings(body, amendment=False):
    positions = defaultdict(lambda: [Decimal(0), Decimal(0), ""])
    amendment_type = None
    for part in re.findall(rb"<DOCUMENT>(.*?)</DOCUMENT>", body, re.I | re.S):
        typ = re.search(rb"<TYPE>\s*([^\r\n<]+)", part, re.I)
        if not typ:
            continue
        kind = typ[1].strip().upper()
        if amendment and kind == b"13F-HR/A":
            amendment_type = _xml_block(part).findtext(".//{*}amendmentType", "").strip().upper()
        if kind != b"INFORMATION TABLE":
            continue
        root = _xml_block(part)
        if root.tag.split("}")[-1] != "informationTable":
            raise ValueError("Unexpected 13F information table root")
        for row in root.findall(".//{*}infoTable"):
            cusip = row.findtext("{*}cusip", "").strip().upper()
            option = row.findtext("{*}putCall", "").strip().upper() or "NON_OPTION"
            unit = row.findtext("{*}shrsOrPrnAmt/{*}sshPrnamtType", "").strip().upper()
            if (
                not re.fullmatch("[0-9A-Z*@#]{9}", cusip)
                or option not in {"PUT", "CALL", "NON_OPTION"}
                or unit not in {"SH", "PRN"}
            ):
                raise ValueError("Unknown security identity or units in 13F")
            key = (cusip, option, unit)
            position = positions[key]
            position[0] += Decimal(row.findtext("{*}shrsOrPrnAmt/{*}sshPrnamt"))
            position[1] += Decimal(row.findtext("{*}value"))
            position[2] = row.findtext("{*}nameOfIssuer", "").strip()
    if not positions:
        raise ValueError("13F information table absent or empty")
    return positions, amendment_type


def _holdings_at(ctx, cik, rows, headers):
    holdings, urls = {}, []
    for row in sorted(rows, key=lambda r: (r["filingDate"], r["acceptanceDateTime"], r["accessionNumber"])):
        acc = row["accessionNumber"]
        url = _folder(cik, row) + acc + ".txt"
        positions, change = parse_holdings(ctx.http.get(url, headers=headers), row["form"].endswith("/A"))
        urls.append(url)
        if row["form"] == "13F-HR" or change == "RESTATEMENT":
            holdings = positions
        elif change == "NEW HOLDINGS" and holdings:
            for key, (shares, value, issuer) in positions.items():
                old = holdings.get(key, [Decimal(0), Decimal(0), issuer])
                holdings[key] = [old[0] + shares, old[1] + value, issuer]
        else:
            raise ValueError(f"Unsupported amendment chain ({change}) in {acc}")
    return holdings, urls


def _changes(before, after, period, vintage):
    totals = [sum(row[1] for row in side.values()) for side in (before, after)]
    changes, facts = [], []
    for key in sorted(set(before) | set(after)):
        old = before.get(key, [Decimal(0), Decimal(0), ""])
        new = after.get(key, [Decimal(0), Decimal(0), ""])
        change = new[0] - old[0]
        action = (
            "new"
            if key not in before
            else (
                "exit"
                if key not in after
                else "increased" if change > 0 else "decreased" if change < 0 else "unchanged"
            )
        )
        label = " ".join(key)
        weights = [v[1] * 100 / total if total else None for v, total in zip((old, new), totals)]
        changes.append(
            {
                "cusip": key[0],
                "option": key[1],
                "unit": key[2],
                "issuer": new[2] or old[2],
                "status": action,
                "previous_quantity": str(old[0]),
                "quantity": str(new[0]),
                "quantity_change": str(change),
                "previous_weight_pct": str(round(weights[0], 5)) if weights[0] is not None else None,
                "weight_pct": str(round(weights[1], 5)) if weights[1] is not None else None,
            }
        )
        if all(w is not None for w in weights):
            changes[-1]["weight_change_pct_points"] = str(round(weights[1] - weights[0], 5))
        if change:
            facts.append(Fact(f"quantity_change {label}", str(change), key[2], period, vintage))
    return changes, facts


def _institutions(ctx, conf, source, headers):
    tickers = {a["symbol"]: a["id"] for a in ctx.watchlist["assets"]}
    for manager, limit in _slices(ctx, ctx.watchlist.get("managers", conf["managers"])):
        cik = _cik(manager["cik"])
        try:
            if not cik:
                raise ValueError("Manager requires a valid CIK")
            _, rows = _submissions(ctx, cik, headers)
            groups = defaultdict(list)
            for row in rows:
                if row["form"] in ("13F-HR", "13F-HR/A") and row["reportDate"]:
                    groups[row["reportDate"]].append(row)
            periods = sorted(groups)
            if len(periods) < 2:
                ctx.warn(f"{manager['name']}: two reports unavailable")
                continue
            pairs = list(zip(periods[:-1], periods[1:]))
            if ctx.backfill:
                floor = (ctx.now.date() - timedelta(days=int(conf.get("backfill_days", 365)))).isoformat()
                pairs = [(p, c) for p, c in pairs if c >= floor and c > ctx.cursor.get(f"backfill:{cik}", "")]
            else:
                pairs = pairs[-1:]
            snapshots = {}
            for prev, current in pairs[:limit]:
                a, b = map(date.fromisoformat, (prev, current))
                if not 80 <= (b - a).days <= 100:
                    ctx.warn(f"{manager['name']}: nonconsecutive reports {prev} / {current}")
                    continue
                signature = ":".join(sorted(r["accessionNumber"] for p in (prev, current) for r in groups[p]))
                key = f"{cik}:{current}"
                if not ctx.backfill and ctx.cursor.get(key) == signature:
                    continue
                for period in (prev, current):
                    if period not in snapshots:
                        snapshots[period] = _holdings_at(ctx, cik, groups[period], headers)
                before, urls1 = snapshots[prev]
                after, urls2 = snapshots[current]
                last = max(
                    groups[prev] + groups[current],
                    key=lambda r: (r["filingDate"], r["acceptanceDateTime"], r["accessionNumber"]),
                )
                published = _published(last)
                changes, facts = _changes(before, after, current, published or last["filingDate"])
                present = {c["cusip"] for c in changes}
                assets = sorted(
                    {
                        tickers[s]
                        for cusip, s in conf.get("cusips", {}).items()
                        if cusip in present and s in tickers
                    }
                )
                changed = [c for c in changes if c["status"] != "unchanged"]
                title = f"{manager['name']} quarterly 13F comparison"
                summary = f"{len(changed)} of {len(changes)} disclosed security/option lines changed quantity from {prev} to {current}."
                if changed:
                    sample = next((c for c in changed if c["cusip"] in conf.get("cusips", {})), changed[0])
                    summary += f" {sample['issuer']} {sample['option']}: {sample['previous_quantity']} to {sample['quantity']} {sample['unit']}."
                yield Record(
                    source_id=source["id"],
                    external_id=key,
                    url=urls2[-1],
                    module="corporate",
                    kind="13F",
                    event_key=f"13f:{key}",
                    title=title,
                    summary=summary,
                    event_at=published or _at_day(last["filingDate"]),
                    published_at=published,
                    assets=tuple(assets),
                    facts=tuple(facts),
                    importance=0.76,
                    metadata={
                        "positions": changes,
                        "comparison_period": prev,
                        "report_period": current,
                        "comparison_evidence": urls1 + urls2,
                        "issuer_cik": cik,
                        "accessions": signature,
                        "weight_basis": "reported 13F table value",
                    },
                )
                ctx.next_cursor[key] = signature
            if ctx.backfill:
                if len(pairs) > limit:
                    ctx.next_cursor[f"backfill:{cik}"] = pairs[limit - 1][1]
                else:
                    ctx.next_cursor.pop(f"backfill:{cik}", None)
        except (
            ValueError,
            KeyError,
            IndexError,
            ArithmeticError,
            OSError,
            RuntimeError,
            ET.ParseError,
        ) as exc:
            ctx.warn(f"13F {manager['name']} unavailable: {exc}")


def _calendar(ctx, conf, source):
    issuers = _issuers(ctx)
    today = ctx.now.astimezone(NY).date()
    for feed in conf["earnings_feeds"]:
        cik = _cik(feed["cik"])
        if cik not in issuers:
            continue
        try:
            xml = _safe_xml(ctx.http.get(feed["url"]))
            if xml.find("channel") is None:
                raise ValueError("Missing earnings RSS channel")
            for item in xml.findall("./channel/item"):
                title = item.findtext("title", "")
                if not re.search("financial results|earnings|quarter.*results", title, re.I):
                    continue
                match = re.match(r"(\d{1,2}/\d{1,2}/\d{4})\s*:\s*(.*)", title)
                if not match:
                    raise ValueError("Earnings feed changed its date format")
                day = datetime.strptime(match[1], "%m/%d/%Y").date()
                if not today <= day <= today + timedelta(days=60):
                    continue
                url = item.findtext("link", "")
                guid = item.findtext("guid") or url
                if urlsplit(url).hostname != urlsplit(feed["url"]).hostname:
                    raise ValueError("Unexpected earnings event link")
                pub = item.findtext("pubDate")
                pub = parsedate_to_datetime(pub).astimezone(UTC).isoformat() if pub else None
                # RSS announces only the date. A late check is a polling policy, not a release time.
                check = datetime.combine(day, time(18), NY).astimezone(UTC).isoformat()
                yield Record(
                    source_id=source["id"],
                    external_id=guid,
                    url=url,
                    module="corporate",
                    kind="earnings",
                    event_key=f"earnings:{cik}:{guid}",
                    title=match[2],
                    summary=f"IR lists {match[2]} on {day}; time unavailable.",
                    event_at=_at_day(day.isoformat()),
                    published_at=pub,
                    status="scheduled",
                    assets=_asset_ids(issuers[cik], day.isoformat()),
                    importance=0.7,
                    metadata={
                        "issuer_cik": cik,
                        "date_precision": "day",
                        "release_date": day.isoformat(),
                        "check_at": check,
                        "check_source": "earnings_results",
                        "check_policy": "18:00 ET on confirmed date",
                        "schedule_confidence": "confirmed date only",
                    },
                )
        except (ValueError, KeyError, OSError, RuntimeError, ET.ParseError) as exc:
            ctx.warn(f"Earnings calendar {cik} unavailable: {exc}")
    for event in ctx.watchlist.get("earnings", []):
        cik = _cik(event["cik"])
        if cik not in issuers:
            continue
        at = _stamp(event["scheduled_at"])
        if datetime.fromisoformat(at) < ctx.now - timedelta(days=1):
            continue
        if urlsplit(event["url"]).scheme != "https":
            raise ValueError("Earnings entries require an HTTPS evidence link")
        yield Record(
            source_id=source["id"],
            external_id=event["id"],
            url=event["url"],
            module="corporate",
            kind="earnings",
            event_key=f"earnings:{cik}:{event['id']}",
            title=event["title"],
            summary=f"Confirmed earnings event at {at}.",
            event_at=at,
            published_at=None,
            status="scheduled",
            assets=_asset_ids(issuers[cik], at[:10]),
            importance=0.7,
            scheduled_at=at,
            metadata={
                "issuer_cik": cik,
                "check_source": "earnings_results",
                "schedule_confidence": "confirmed",
                "release_date": datetime.fromisoformat(at).astimezone(NY).date().isoformat(),
            },
        )


def _earnings_results(ctx, source, headers):
    issuers = _issuers(ctx, headers)
    for event in ctx.due:
        meta = event["metadata"]
        if meta.get("check_source") != source["id"]:
            continue
        cik = _cik(meta["issuer_cik"])
        if cik not in issuers:
            continue
        sub, rows = _submissions(ctx, cik, headers)
        day = meta["release_date"]
        for row in rows:
            # Require same-day explicit Item 2.02 earnings disclosure; unrelated filings never resolve the timer.
            pub = _published(row)
            if (
                row["form"] not in ("8-K", "8-K/A")
                or "2.02" not in row["items"]
                or not pub
                or datetime.fromisoformat(pub).astimezone(NY).date().isoformat() != day
            ):
                continue
            filing = _filing(ctx, cik, sub["name"], _asset_ids(issuers[cik], day), row, headers, {})
            yield replace(
                filing,
                source_id=source["id"],
                external_id=row["accessionNumber"] + ":" + event["event_key"],
                kind="earnings",
                event_key=event["event_key"],
                title=f"{sub['name']} earnings disclosure",
                status="published",
                importance=0.9,
                metadata={**meta, "filing_accession": row["accessionNumber"]},
            )
            break


def collect(ctx, source):
    conf = ctx.config("corporate")
    if source["id"] == "earnings_calendar":
        yield from _calendar(ctx, conf, source)
        return
    headers = _sec_headers(ctx)
    if source["id"] == "sec_filings":
        yield from _filings(ctx, conf, headers)
    elif source["id"] == "sec_13f":
        yield from _institutions(ctx, conf, source, headers)
    elif source["id"] == "earnings_results":
        yield from _earnings_results(ctx, source, headers)
    else:
        raise ValueError(f"Unknown corporate source {source['id']}")


def demo_response(url, method, payload, phase):
    from ..fixtures.corporate_demo import response

    return response(url, phase)
