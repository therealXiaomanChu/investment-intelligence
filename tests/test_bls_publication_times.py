from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from investment_intelligence.collectors import bls_publications
from investment_intelligence.collectors.bls_publications import DOL_INDEX, _release_clock, verified_release
from investment_intelligence.http import SourceError


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


@pytest.mark.parametrize("index_accessible", [True, False])
def test_dol_mirror_can_verify_publication_when_bls_feed_is_unavailable(monkeypatch, index_accessible):
    dol_pdf = DOL_INDEX + "/cpi_11102026.pdf"
    archive = Mock()
    archive.all.return_value = [{"event_at": "2026-11-10T13:30:00+00:00",
                                 "canonical_url": "https://www.newyorkfed.org/research/calendars/i-nov26.html"}]
    def fetch(url):
        if url == DOL_INDEX and index_accessible:
            return b'<a href="/newsroom/economicdata/cpi_11102026.pdf">Consumer Price Index</a>'
        if url == dol_pdf:
            return b"%PDF-synthetic"
        raise SourceError("source HTTP 403")

    page = SimpleNamespace(extract_text=lambda: REPORT)
    monkeypatch.setattr(bls_publications, "PdfReader", lambda _: SimpleNamespace(pages=[page]))
    ctx = SimpleNamespace(http=Mock(get=Mock(side_effect=fetch)), now=CHECKED, archive=archive, warn=Mock())
    state, feeds, checked = {}, {}, set()
    assert verified_release(ctx, "cpi", "2026-10", state, feeds, checked) == {
        "at": "2026-11-10T08:30:00-05:00", "url": dol_pdf,
    }
    assert archive.all.called == (not index_accessible)
    assert [call.args[0] for call in ctx.http.get.call_args_list].count(DOL_INDEX) == 1
    assert not ctx.warn.called


def test_only_recent_unverified_candidates_warn_and_never_become_publications():
    def fetch(url):
        if url == DOL_INDEX:
            return b'<a href="/newsroom/economicdata/cpi_11102026.pdf">Consumer Price Index</a>'
        raise SourceError("source HTTP 403")
    ctx = SimpleNamespace(http=Mock(get=Mock(side_effect=fetch)), now=CHECKED, warn=Mock())
    state, feeds, checked = {}, {}, set()
    assert verified_release(ctx, "cpi", "2026-10", state, feeds, checked) is None
    assert verified_release(ctx, "cpi", "2026-10", state, feeds, checked) is None
    assert ctx.warn.call_count == 1 and "recent release clock unverified" in ctx.warn.call_args.args[0]
    assert state["unverified_publications"]["bls:cpi:2026-10"]["candidate_days"] == ["2026-11-10"]
    assert not state["publications"]
    ctx.warn.reset_mock()
    ctx.now = CHECKED.replace(day=20)
    assert verified_release(ctx, "cpi", "2026-10", state, {}, set()) is None
    assert not ctx.warn.called
