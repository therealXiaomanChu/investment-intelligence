"""Governance facts from NVIDIA's own press releases."""

import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup


def is_release_url(url):
    parts = urlsplit(url)
    return (
        parts.scheme == "https"
        and parts.netloc.lower() == "nvidianews.nvidia.com"
        and re.fullmatch(r"/news/[a-zA-Z0-9_-]+/?", parts.path) is not None
    )


def release_text(node):
    # The publisher embeds HTML inside a second, literal CDATA wrapper.
    body = (node.findtext("content") or "").strip()
    if body.startswith("<![CDATA[") and body.endswith("]]>"):
        body = body[9:-3]
    soup = BeautifulSoup(body, "html.parser")
    for element in soup(["script", "style", "blockquote", "aside"]):
        element.decompose()
    text = soup.get_text(" ", strip=True) or node.findtext("description") or ""
    return " ".join(((node.findtext("subtitle") or "") + " " + text).split())


_BOARD = r"(?:the )?(?:independent members of the nvidia board|nvidia(?:'s)? board(?: of directors)?)"
_INQUIRY = r"(?:an? |its |a thorough and )?independent (?:investigation|inquiry|review)"
_ROLE = r"(?:chief (?:executive|financial|operating|technology) officer|ceo|cfo|coo|cto|president)"
_PERSON = r"[A-Z][\w.'’-]+(?: [A-Z][\w.'’-]+){1,4}"


def classify(title, body):
    """Return only an affirmative issuer action, not an allegation about an executive."""
    headline = title.replace("’", "'")
    title = headline.casefold()
    lead = body[:4000].casefold().replace("’", "'")
    board_title = re.fullmatch(r"(?:statement|update|announcement) from " + _BOARD, title) or re.match(
        _BOARD + r" (?:announces|opens|initiates|launches|retains|appoints)\b", title
    )
    if board_title and re.match(
        _BOARD + r" (?:is initiating|has initiated|initiated|initiates|is launching|has launched|launches|opens|has opened) "
        + _INQUIRY, lead
    ):
        return "board_investigation_opened", 0.94, "NVIDIA's board announced an independent investigation."
    if (
        board_title
        and re.match(_BOARD + r" (?:have|has) (?:retained|appointed)\b", lead)
        and re.search(r"\b(?:counsel|attorneys?|law firm|lawyers?)\b", lead)
        and re.search(r"\bto (?:conduct|lead) " + _INQUIRY, lead)
    ):
        return (
            "investigation_counsel_retained",
            0.93,
            "NVIDIA announced the retention of counsel to conduct its board investigation.",
        )
    if re.search(
        r"\bnvidia (?:founder and )?(?:ceo|chief executive officer)\b.{1,60}\bsent\b.{1,60}\b(?:email|message)\b.{1,40}\bemployees\b",
        lead,
    ) and (
        re.search(_BOARD + r" has initiated " + _INQUIRY, lead)
        and re.search(r"\bi support the investigation and will cooperate\b", lead)
    ):
        return (
            "ceo_cooperation_statement",
            0.87,
            "NVIDIA published its CEO's message to employees confirming support for and cooperation with the board investigation.",
        )
    if re.search(
        r"\b(?:rumou?rs?|may|might|reportedly|considers|could|denies|partner|customer|supplier|subsidiary)\b",
        title,
    ):
        return None
    if re.fullmatch(
        r"(?i:NVIDIA (?:names|appoints|elects)) "
        + _PERSON
        + r" (?i:(?:as |to serve as )?(?:its |nvidia's )?(?:new |next |interim )?"
        + _ROLE
        + r"(?: of nvidia)?)[.!]?",
        headline,
    ):
        return "executive_appointment", 0.9, "NVIDIA announced a senior executive appointment."
    if re.match(
        r"(?i:NVIDIA(?:'s)? "
        + _ROLE
        + r")(?: "
        + _PERSON
        + r")? (?i:resigns|retires|steps down|to retire|to step down)(?:$|[,.])",
        headline,
    ):
        return "executive_departure", 0.9, "NVIDIA announced a senior executive's departure."
    return None
