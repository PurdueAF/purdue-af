"""Fixtures for the supersonic-model-manager suite.

The service is a package (``model_manager``) living under docker/, not an
installed distribution — make its directory importable before anything else.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "docker" / "supersonic-model-manager")
)

from model_manager import repository  # noqa: E402
from model_manager.config import settings  # noqa: E402


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """An empty model repository rooted in a temp dir."""
    root = tmp_path / "models"
    root.mkdir()
    monkeypatch.setattr(settings, "repository_path", str(root))
    return root


@pytest.fixture
def make_model(repo):
    """Create a well-formed Triton model directory inside the repository."""

    def _make(
        name,
        platform="onnxruntime_onnx",
        versions=("1",),
        artifact="model.onnx",
        size=32,
        config=True,
        parent=None,
    ):
        base = (parent or repo) / name
        base.mkdir(parents=True, exist_ok=True)
        if config:
            (base / "config.pbtxt").write_text(
                f'name: "{name}"\nplatform: "{platform}"\nmax_batch_size: 8\n'
            )
        for version in versions:
            vdir = base / version
            vdir.mkdir(exist_ok=True)
            if artifact:
                (vdir / artifact).write_bytes(b"\0" * size)
        return base

    return _make


@pytest.fixture
def staged(tmp_path):
    """A staging area outside the repository, for validation tests."""
    staging = tmp_path / "staging"
    staging.mkdir()
    return staging


__all__ = ["repository", "settings"]
