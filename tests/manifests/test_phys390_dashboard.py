"""The PHYS 390 display is the default dashboard's Facility status section, laid out to fill one screen.

Its panels are copies, so a panel edited on the default dashboard alone would
leave the display showing the old one.
"""

import json

from common import REPO

DASHBOARDS = REPO / "apps/monitoring/grafana/dashboards"

# Shown without scrolling on a 1080p screen in kiosk mode.
SCREEN_ROWS = 26
NOT_SHOWN = {"Alerts"}


def _panels(name):
    return json.loads((DASHBOARDS / name).read_text())["panels"]


def _facility_status():
    """The default dashboard's panels between the Facility status row and the next row."""
    section, inside = {}, False
    for panel in _panels("default.json"):
        if panel["type"] == "row":
            inside = panel["title"] == "Facility status"
        elif inside:
            section[panel["title"]] = panel
    return section


def _content(panel):
    """A panel without its position, size and text size: those are the display's own."""
    content = {k: v for k, v in panel.items() if k != "gridPos"}
    content["options"] = {k: v for k, v in panel["options"].items() if k != "text"}
    return content


def test_display_shows_the_facility_status_panels_unchanged():
    display = {p["title"]: p for p in _panels("phys390.json") if p["type"] != "row"}
    section = _facility_status()
    assert set(display) == set(section) - NOT_SHOWN
    for title, panel in display.items():
        assert _content(panel) == _content(section[title]), title


def test_display_fits_one_screen():
    panels = _panels("phys390.json")
    assert max(p["gridPos"]["y"] + p["gridPos"]["h"] for p in panels) <= SCREEN_ROWS
    assert all(p["gridPos"]["x"] + p["gridPos"]["w"] <= 24 for p in panels)
