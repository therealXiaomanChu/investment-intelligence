from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from investment_intelligence.collectors.bls_publications import _release_clock, verified_release


DAY = date(2026, 11, 10)
CHECKED = datetime(2026, 11, 10, 14, tzinfo=timezone.utc)
REPORT = (
    "Transmission of material in this release is embargoed until USDL-26-1685\n"
    "8:30 a.m. (ET) Tuesday, November 10, 2026\nCONSUMER PRICE INDEX – OCTOBER 2026"
)
ARTICLE = "https://www.bls.gov/news.release/archives/cpi_11102026.htm"
FEED = f"""<feed xmlns="http://www.w3.org/2005/Atom"><id>bls.gov:feed:cpi</id>
<entry><title>CPI for October</title><published>2026-11-10T07:55:47-05:00</published>
<link href="{ARTICLE}"/></entry></feed>""".encode()


class Transport:
    def __init__(self):
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        if url == "https://www.bls.gov/feed/cpi.rss":
            return FEED
        if url == ARTICLE:
            return ("<pre>" + REPORT + "</pre>").encode()
        return b""


def test_release_period_and_embargo_override_earlier_atom_preparation_time():
    ctx = SimpleNamespace(http=Transport(), now=CHECKED.replace(hour=13, minute=20))
    state = {}
    assert verified_release(ctx, "cpi", "2026-10", state, {}, set()) is None
    ctx.now = CHECKED
    assert verified_release(ctx, "cpi", "2026-09", state, {}, set()) is None
    actual = verified_release(ctx, "cpi", "2026-10", state, {}, set())
    assert actual == {"at": "2026-11-10T08:30:00-05:00", "url": ARTICLE}
    ctx.http.calls.clear()
    assert verified_release(ctx, "cpi", "2026-10", state, {}, set()) == actual
    assert not ctx.http.calls


@pytest.mark.parametrize("text", [
    REPORT.replace("CONSUMER PRICE INDEX", "THE EMPLOYMENT SITUATION"),
    REPORT.replace("embargoed until", "scheduled for"),
    REPORT.replace("Tuesday", "Wednesday"),
    REPORT.replace("November 10", "November 3"),
])
def test_unverified_group_or_archive_date_cannot_supply_publication(text):
    assert _release_clock(text, "Consumer Price Index", DAY, CHECKED) is None
