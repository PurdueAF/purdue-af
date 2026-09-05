"""Fixtures for the sonic-ray suite.

The server is a package (``sonic_ray``) living in the chart's files/, not an
installed distribution — make its directory importable before anything else.
The models are built in sonic_ray_helpers with onnx.helper, so the suite
needs no fixture files and no GPU: the CPU build of onnxruntime runs them.
"""

import sys
from pathlib import Path

import onnx
import pytest

sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parents[2]
        / "apps"
        / "ray"
        / "sonic-ray"
        / "chart"
        / "files"
    ),
)

from sonic_ray_helpers import doubler_model, particlenet_like_model  # noqa: E402


@pytest.fixture
def repo(tmp_path):
    """An empty model repository rooted in a temp dir."""
    root = tmp_path / "models"
    root.mkdir()
    return root


@pytest.fixture
def make_model(repo):
    """Lay out one Triton-style model directory: version dirs + artifact."""

    def _make(name, versions=("1",), artifact="model.onnx", model=None):
        model_dir = repo / name
        model_dir.mkdir()
        for version in versions:
            (model_dir / version).mkdir()
            if model is not None:
                onnx.save(model, model_dir / version / artifact)
            else:
                (model_dir / version / artifact).write_bytes(b"not a model")
        return model_dir

    return _make


@pytest.fixture
def store(make_model, repo):
    """A repository shaped like the AF's: ONNX models that load, a TensorFlow
    one that does not."""
    from sonic_ray.models import ModelStore

    make_model("particleNetFromMiniAODAK8", model=particlenet_like_model())
    make_model("doubler", model=doubler_model())
    make_model("deepmet", versions=("1", "2", "3"), artifact="model.graphdef")
    return ModelStore(repo, providers=("CPUExecutionProvider",))
