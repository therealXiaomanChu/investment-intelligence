"""Synthetic wire responses. These numbers and accessions do not describe actual investments."""

import json
from urllib.parse import urlsplit

NV = "0001045810"
SA = "0002045724"
Q = NV + "-26-990001"
AMEND = NV + "-26-990002"
CURRENT = NV + "-26-990003"
PREVIOUS_13F = SA + "-26-990001"
LATEST_13F = SA + "-26-990002"
SYMBOLS = {
    "0001652044": ["GOOG", "GOOGL"],
    "0001326801": ["META"],
    "0000050863": ["INTC"],
    "0000320193": ["AAPL"],
    NV: ["NVDA"],
    "0001318605": ["TSLA"],
    "0001181412": ["SPCX"],
}


def _filing(form, accession, filed, period, document, items=""):
    return dict(
        form=form,
        accessionNumber=accession,
        filingDate=filed,
        reportDate=period,
        primaryDocument=document,
        acceptanceDateTime=filed + "T12:00:00Z",
        items=items,
    )


def _submissions(cik, phase):
    filings = []
    if cik == NV:
        filings.append(_filing("10-Q", Q, "2026-10-28", "2026-09-30", "sample-q.htm"))
        if phase:
            filings.append(_filing("10-Q/A", AMEND, "2026-10-30", "2026-09-30", "sample-amend.htm"))
            filings.append(_filing("8-K", CURRENT, "2026-11-02", "2026-11-02", "sample-8k.htm", "2.02,9.01"))
    if cik == SA:
        filings.extend(
            [
                _filing("13F-HR", PREVIOUS_13F, "2026-08-12", "2026-06-30", "primary_doc.xml"),
                _filing("13F-HR", LATEST_13F, "2026-10-29", "2026-09-30", "primary_doc.xml"),
            ]
        )
    cols = {
        key: [row[key] for row in reversed(filings)]
        for key in (
            "form",
            "accessionNumber",
            "filingDate",
            "reportDate",
            "primaryDocument",
            "acceptanceDateTime",
            "items",
        )
    }
    return {
        "filings": {"recent": cols, "files": []},
        "cik": int(cik),
        "name": "NVIDIA (synthetic demo)" if cik == NV else "Demo filer",
        "tickers": SYMBOLS.get(cik, []),
    }


def _financials():
    concepts = {}
    for name, val in [
        ("Revenues", 100000),
        ("GrossProfit", 65000),
        ("OperatingIncomeLoss", 45000),
        ("NetCashProvidedByUsedInOperatingActivities", 50000),
        ("PaymentsToAcquirePropertyPlantAndEquipment", 10000),
    ]:
        units = []
        for acc in [Q, AMEND]:
            units.append(
                dict(
                    accn=acc,
                    start="2026-07-01",
                    end="2026-09-30",
                    val=val + (5000 if acc == AMEND and name == "Revenues" else 0),
                )
            )
            units.append(dict(accn=acc, start="2025-07-01", end="2025-09-30", val=80000))
        concepts[name] = {"units": {"USD": units}}
    return {"cik": int(NV), "facts": {"us-gaap": concepts}}


def _holdings(new):
    content = []
    for shares, value, option in [
        (120 if new else 100, 24000 if new else 18000, ""),
        (10 if new else 30, 2000 if new else 5400, "Put"),
    ]:
        content.append(
            f"""<infoTable><nameOfIssuer>Demo issuer NVDA</nameOfIssuer><cusip>67066G104</cusip>
        <value>{value}</value><shrsOrPrnAmt><sshPrnamt>{shares}</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
        {f'<putCall>{option}</putCall>' if option else ''}</infoTable>"""
        )
    return (
        "<SEC-DOCUMENT><DOCUMENT>\n<TYPE>INFORMATION TABLE\n<XML>\n<informationTable xmlns='urn:demo'>"
        + "".join(content)
        + "</informationTable>\n</XML>\n</DOCUMENT></SEC-DOCUMENT>"
    ).encode()


def response(url, phase):
    host, path = urlsplit(url).hostname, urlsplit(url).path
    if host == "investor.nvidia.com" and path == "/rss/event.aspx":
        return b"""<rss><channel><item><title>11/02/2026 : NVIDIA synthetic earnings event</title>
        <guid>demo-earnings</guid><pubDate>Mon, 26 Oct 2026 12:00:00 -0400</pubDate>
        <link>https://investor.nvidia.com/demo-earnings</link></item></channel></rss>"""
    if host == "data.sec.gov" and path.startswith("/submissions/CIK"):
        return json.dumps(_submissions(path.split("CIK")[1][:10], phase)).encode()
    if host == "data.sec.gov" and path == f"/api/xbrl/companyfacts/CIK{NV}.json":
        return json.dumps(_financials()).encode()
    if (
        host == "www.sec.gov"
        and path.endswith(".txt")
        and path.split("/")[-1][:-4] in (PREVIOUS_13F, LATEST_13F)
    ):
        return _holdings(LATEST_13F in path)
    if host == "www.sec.gov" and path.endswith(("sample-q.htm", "sample-amend.htm", "sample-8k.htm")):
        return (
            "<html><body>Synthetic SEC fixture "
            + path.rsplit("/", 1)[1]
            + " Item 2.02. Results of Operations and Financial Condition.</body></html>"
        ).encode()
    return None
