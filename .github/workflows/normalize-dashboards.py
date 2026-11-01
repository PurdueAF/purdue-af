#!/usr/bin/env python3
"""Strip what a Grafana export restates from the dashboards it is given.

A file changes only when its content does, so prettier keeps the formatting.
Output stays indented ASCII: Flux envsubst truncates a line over 64 KB.
"""

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

MIXED = "-- Mixed --"

# The defaults of the timeseries panel in Grafana 13.2, from
# public/app/plugins/panel/timeseries/config.ts and grafana-ui's axis options.
TIMESERIES_DEFAULTS: dict[str, Any] = {
    "axisBorderShow": False,
    "axisCenteredZero": False,
    "axisColorMode": "text",
    "axisLabel": "",
    "axisPlacement": "auto",
    "barAlignment": 0,
    "barWidthFactor": 0.6,
    "drawStyle": "line",
    "fillOpacity": 0,
    "gradientMode": "none",
    "hideFrom": {"legend": False, "tooltip": False, "viz": False},
    "insertNulls": False,
    "lineInterpolation": "linear",
    "lineWidth": 1,
    "pointSize": 5,
    "scaleDistribution": {"type": "linear"},
    "showPoints": "auto",
    "showValues": False,
    "spanNulls": False,
    "stacking": {"group": "A", "mode": "none"},
    "thresholdsStyle": {"mode": "off"},
}


def panels(container: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for panel in container.get("panels", []):
        yield panel
        yield from panels(panel)


def is_set(defaults: dict[str, Any], path: str) -> bool:
    node: Any = defaults
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def strip_override_properties(panel: dict[str, Any]) -> None:
    """A property with no value clears the path it names: it is noise only
    where nothing before it set that path."""
    field_config = panel.get("fieldConfig", {})
    defaults = field_config.get("defaults", {})
    earlier: set[str] = set()
    for override in field_config.get("overrides", []):
        kept = []
        for prop in override.get("properties", []):
            if "value" in prop:
                earlier.add(prop["id"])
            elif prop["id"] in earlier or is_set(defaults, prop["id"]):
                earlier.discard(prop["id"])
            else:
                continue
            kept.append(prop)
        override["properties"] = kept
    field_config_overrides = field_config.get("overrides", [])
    if field_config_overrides:
        field_config["overrides"] = [
            override
            for override in field_config_overrides
            if override.get("properties")
        ]


def drop_if(owner: dict[str, Any], key: str, empty: Any) -> None:
    if key in owner and owner[key] == empty:
        del owner[key]


def strip_datasources(panel: dict[str, Any]) -> None:
    datasource = panel.get("datasource")
    targets = panel.get("targets", [])
    if isinstance(datasource, dict) and datasource.get("uid") == MIXED:
        used = [t.get("datasource") for t in targets]
        if used and all(u == used[0] for u in used) and used[0] is not None:
            panel["datasource"] = datasource = used[0]
    for target in targets:
        if datasource is not None and target.get("datasource") == datasource:
            del target["datasource"]


def normalize_panel(panel: dict[str, Any]) -> None:
    panel.pop("pluginVersion", None)
    drop_if(panel, "description", "")
    strip_datasources(panel)
    for target in panel.get("targets", []):
        drop_if(target, "interval", "")
    strip_override_properties(panel)
    field_config = panel.get("fieldConfig")
    if field_config is None:
        return
    drop_if(field_config, "overrides", [])
    defaults = field_config.get("defaults", {})
    drop_if(defaults, "mappings", [])
    custom = defaults.get("custom")
    if panel.get("type") == "timeseries" and custom is not None:
        for key, value in TIMESERIES_DEFAULTS.items():
            drop_if(custom, key, value)
        drop_if(defaults, "custom", {})


def normalize(dashboard: dict[str, Any]) -> None:
    dashboard.pop("id", None)
    dashboard.pop("version", None)
    for panel in panels(dashboard):
        normalize_panel(panel)


def main(paths: list[str]) -> None:
    for name in paths:
        path = Path(name)
        before = json.loads(path.read_text())
        after = json.loads(json.dumps(before))
        normalize(after)
        if after != before:
            path.write_text(json.dumps(after, indent=2) + "\n")
            print(f"normalized {path}")


if __name__ == "__main__":
    main(sys.argv[1:])
