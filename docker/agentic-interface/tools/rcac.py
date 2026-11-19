"""Outages and maintenance announced by RCAC, which runs Geddes, Depot and the
campus systems the facility stands on."""

import html
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import httpx
from annotations import FACILITY, READS
from errors import UserError, http_error, malformed_response, unreachable
from mcp.server.mcpserver import MCPServer
from shared import shared_client

from tools.health import EASTERN

FEED_URL = "https://www.rcac.purdue.edu/news/rss/Outages%20and%20Maintenance"
NEWS_URL = "https://www.rcac.purdue.edu/news/outages-and-maintenance"
SERVICE = "RCAC news"
_CACHE_SECONDS = 300.0
_TEXT_CHARS = 700
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
# When the cached items expire, and the items; None until the first read.
_cached: tuple[float, list[dict]] | None = None


def _text(fragment: str) -> str:
    text = _SPACE.sub(" ", html.unescape(_TAG.sub(" ", fragment))).strip()
    return text if len(text) <= _TEXT_CHARS else text[:_TEXT_CHARS].rstrip() + " …"


def parse(feed: str) -> list[dict]:
    """The feed's items, newest first: title, link, date and plain text."""
    items = []
    for item in ET.fromstring(feed).iter("item"):
        try:
            when = parsedate_to_datetime(item.findtext("pubDate") or "")
        except (TypeError, ValueError):
            continue
        items.append(
            {
                "title": (item.findtext("title") or "").strip(),
                "link": (item.findtext("link") or "").strip(),
                "when": when,
                "text": _text(item.findtext("description") or ""),
            }
        )
    return sorted(items, key=lambda i: i["when"], reverse=True)


async def _items() -> list[dict]:
    global _cached
    if _cached and time.monotonic() < _cached[0]:
        return _cached[1]
    try:
        resp = await shared_client("rcac").get(FEED_URL, timeout=15.0)
    except httpx.HTTPError as exc:
        raise unreachable(SERVICE, exc, next_step=f"Its page is {NEWS_URL}.") from exc
    if resp.status_code != 200:
        raise http_error(SERVICE, resp, next_step=f"Its page is {NEWS_URL}.")
    try:
        items = parse(resp.text)
    except ET.ParseError as exc:
        raise malformed_response(SERVICE, resp, "an RSS feed") from exc
    _cached = (time.monotonic() + _CACHE_SECONDS, items)
    return items


def register(mcp: MCPServer) -> None:
    @mcp.tool(annotations=READS, meta=FACILITY)
    async def get_rcac_outages(days: int = 14) -> str:
        """Outages and maintenance announced by RCAC, Purdue's research computing
        center, which runs Geddes (the cluster under the facility), Depot,
        Fortress, the campus clusters and their network.

        Read it alongside get_facility_health whenever something is down, slow
        or unreachable, and for planned maintenance: an announcement names the
        cause and the expected end, which the facility's own monitoring cannot.

        Args:
            days: How far back to list announcements, 1 to 90. Planned ones
                  dated in the future are always listed.
        """
        if not 1 <= days <= 90:
            raise UserError("Error: days must be from 1 to 90.")
        since = datetime.now(tz=timezone.utc) - timedelta(days=days)
        recent = [i for i in await _items() if i["when"] >= since]
        if not recent:
            return f"RCAC has announced no outages or maintenance in the last {days} days ({NEWS_URL})."
        lines = [
            f"RCAC outages and maintenance, newest first, dated as RCAC gives them "
            f"(the start of an outage, or when it was posted). All: {NEWS_URL}\n"
        ]
        for item in recent:
            when = item["when"].astimezone(EASTERN)
            lines.append(
                f"- {when:%d %b %Y, %H:%M} ET: {item['title']} ({item['link']})\n"
                f"  {item['text']}"
            )
        return "\n".join(lines)
