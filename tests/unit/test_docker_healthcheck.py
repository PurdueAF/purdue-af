from __future__ import annotations

import json
from types import ModuleType


class _FakeJsonFile:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read_bytes(self) -> bytes:
        return self.payload


class _FakePath:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __truediv__(self, _part: str) -> "_FakePath":
        return self

    def glob(self, _pattern: str):
        return iter([_FakeJsonFile(self.payload)])


def test_healthcheck_queries_jupyter_api_and_prints_response(monkeypatch, module_loader) -> None:
    captured = {}
    payload = json.dumps({"url": "https://af.example/"}).encode("utf-8")

    class _FakeResponse:
        def __init__(self) -> None:
            self.content = b"healthy"
            self.raise_calls = 0

        def raise_for_status(self) -> None:
            self.raise_calls += 1

    fake_response = _FakeResponse()
    requests_stub = ModuleType("requests")

    def _fake_get(url: str, verify: bool):
        captured["url"] = url
        captured["verify"] = verify
        return fake_response

    requests_stub.get = _fake_get

    pathlib_stub = ModuleType("pathlib")
    pathlib_stub.Path = lambda _value: _FakePath(payload)

    printed = []
    monkeypatch.setenv("NB_USER", "alice")
    monkeypatch.setattr("builtins.print", lambda value: printed.append(value))

    module_loader(
        "docker/purdue-af/jupyter/docker_healthcheck.py",
        extra_modules={"pathlib": pathlib_stub, "requests": requests_stub},
    )

    assert captured == {"url": "https://af.example/api", "verify": False}
    assert fake_response.raise_calls == 1
    assert printed == [b"healthy"]
