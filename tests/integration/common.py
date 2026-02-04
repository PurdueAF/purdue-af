from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_ROOT = REPO_ROOT / "tests" / "fixtures"


class FakeGaugeChild:
    def __init__(self, labels: dict[str, str]):
        self.labels = labels
        self.value: float | int | None = None
        self.history: list[float | int] = []

    def set(self, value: float | int) -> None:
        self.value = value
        self.history.append(value)


class FakeGauge:
    def __init__(
        self,
        name: str,
        description: str,
        label_names: list[str] | tuple[str, ...] | None = None,
    ):
        self.name = name
        self.description = description
        self.label_names = tuple(label_names or ())
        self.value: float | int | None = None
        self.history: list[float | int] = []
        self.children: dict[tuple[tuple[str, str], ...], FakeGaugeChild] = {}

    def set(self, value: float | int) -> None:
        self.value = value
        self.history.append(value)

    def labels(self, *args: str, **kwargs: str) -> FakeGaugeChild:
        if args and kwargs:
            raise ValueError("labels accepts positional or keyword labels, not both")

        if args:
            if len(args) != len(self.label_names):
                raise ValueError("label count does not match")
            label_values = dict(zip(self.label_names, args))
        else:
            label_values = {name: kwargs[name] for name in self.label_names}

        key = tuple((name, label_values[name]) for name in self.label_names)
        child = self.children.get(key)
        if child is None:
            child = FakeGaugeChild(label_values)
            self.children[key] = child
        return child


def load_json_fixture(relative_path: str) -> Any:
    fixture_path = FIXTURES_ROOT / relative_path
    with fixture_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_module_with_fake_prometheus(relative_path: str, module_name: str):
    module_path = REPO_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load module spec for {module_path}")

    module = importlib.util.module_from_spec(spec)

    fake_prometheus = types.ModuleType("prometheus_client")
    fake_prometheus.Gauge = FakeGauge
    fake_prometheus.start_http_server = lambda *_args, **_kwargs: None

    original_prometheus = sys.modules.get("prometheus_client")
    sys.modules["prometheus_client"] = fake_prometheus
    try:
        spec.loader.exec_module(module)
    finally:
        if original_prometheus is None:
            del sys.modules["prometheus_client"]
        else:
            sys.modules["prometheus_client"] = original_prometheus

    return module
