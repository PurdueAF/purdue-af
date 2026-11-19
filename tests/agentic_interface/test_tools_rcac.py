"""Tests for tools/rcac.py — RCAC's outage and maintenance announcements."""

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest
import respx
from agentic_helpers import failure, register_tools
from tools import rcac


def item(title, when, text="<p>Engineers are investigating &amp; will update.</p>"):
    return (
        f"<item><title><![CDATA[{title}]]></title>"
        f"<link>https://www.rcac.purdue.edu/news/{abs(hash(title)) % 9999}</link>"
        f"<description><![CDATA[{text}]]></description>"
        f"<pubDate>{format_datetime(when)}</pubDate></item>"
    )


def feed(*items):
    return f'<?xml version="1.0"?><rss version="2.0"><channel>{"".join(items)}</channel></rss>'


NOW = datetime.now(tz=timezone.utc)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(rcac, "_cached", None)
    monkeypatch.setattr(rcac, "shared_client", lambda name: httpx.AsyncClient())


def outages(days=14):
    return register_tools(rcac).tools["get_rcac_outages"](days=days)


@respx.mock
async def test_recent_and_planned_announcements_are_listed_newest_first(client):
    respx.get(rcac.FEED_URL).respond(
        text=feed(
            item("Geddes Storage Outage", NOW - timedelta(hours=2)),
            item("Old maintenance", NOW - timedelta(days=40)),
            item("Planned Depot maintenance", NOW + timedelta(days=3)),
        )
    )
    out = await outages()
    assert out.index("Planned Depot maintenance") < out.index("Geddes Storage Outage")
    assert "Old maintenance" not in out
    assert "Engineers are investigating & will update." in out
    assert "<p>" not in out


@respx.mock
async def test_a_quiet_period_says_so(client):
    respx.get(rcac.FEED_URL).respond(text=feed(item("Old", NOW - timedelta(days=40))))
    assert "no outages or maintenance in the last 14 days" in await outages()


@respx.mock
async def test_the_feed_is_read_once_per_five_minutes(client):
    route = respx.get(rcac.FEED_URL).respond(text=feed())
    await outages()
    await outages(days=30)
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("respond", "said"),
    [
        ({"side_effect": httpx.ConnectError("down")}, "unreachable"),
        ({"status_code": 503}, "HTTP 503"),
        ({"text": "<html>not a feed"}, "not an RSS feed"),
    ],
)
@respx.mock
async def test_a_broken_feed_says_why_and_where_to_look(client, respond, said):
    route = respx.get(rcac.FEED_URL)
    if "side_effect" in respond:
        route.mock(side_effect=respond["side_effect"])
    else:
        route.respond(**respond)
    message = await failure(outages())
    assert said in message


async def test_days_out_of_range_is_refused(client):
    assert "1 to 90" in await failure(outages(days=0))


def test_long_text_is_cut():
    (parsed,) = rcac.parse(feed(item("Long", NOW, "<p>" + "word " * 500 + "</p>")))
    assert len(parsed["text"]) <= rcac._TEXT_CHARS + 2
    assert parsed["text"].endswith("…")
