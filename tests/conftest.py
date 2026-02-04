from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Callable

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


class RecordingGauge:
    def __init__(self) -> None:
        self.values: list[float] = []
        self.label_children: dict[tuple[tuple[str, str], ...], "RecordingGauge"] = {}

    def set(self, value: float) -> None:
        self.values.append(value)

    def labels(self, **labels: str) -> "RecordingGauge":
        key = tuple(sorted(labels.items()))
        child = self.label_children.get(key)
        if child is None:
            child = RecordingGauge()
            self.label_children[key] = child
        return child


@pytest.fixture
def recording_gauge_cls():
    return RecordingGauge


@pytest.fixture
def prometheus_stub() -> ModuleType:
    module = ModuleType("prometheus_client")

    class Gauge:
        def __init__(self, *_args, **_kwargs) -> None:
            self.values = []

        def set(self, value: float) -> None:
            self.values.append(value)

        def labels(self, **_labels: str) -> "Gauge":
            return self

    module.Gauge = Gauge
    module.start_http_server = lambda *_args, **_kwargs: None
    return module


@pytest.fixture
def module_loader(monkeypatch: pytest.MonkeyPatch) -> Callable[..., object]:
    counter = 0

    def _load(
        relative_path: str, *, extra_modules: dict[str, object] | None = None
    ) -> object:
        nonlocal counter
        counter += 1
        module_name = f"test_module_{counter}"
        module_path = REPO_ROOT / relative_path

        if extra_modules:
            for name, module in extra_modules.items():
                monkeypatch.setitem(sys.modules, name, module)

        spec = importlib.util.spec_from_file_location(module_name, module_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    return _load
