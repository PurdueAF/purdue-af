"""Fixtures for the supersonic-model-manager suite."""

import pytest
from model_manager.config import settings


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
