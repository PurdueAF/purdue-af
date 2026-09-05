"""Fixtures for the sonic-ray suite.

The server is a package (``sonic_ray``) living under docker/, not an
installed distribution — make its directory importable before anything else.
The models are built in sonic_ray_helpers with onnx.helper, so the suite
needs no fixture files and no GPU: the CPU build of onnxruntime runs them.
"""

import sys
from pathlib import Path

import onnx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "docker" / "sonic-ray"))

from sonic_ray_helpers import (  # noqa: E402
    DEEPMET_CONFIG,
    PARTICLENET_CONFIG,
    doubler_model,
    particlenet_like_model,
)


@pytest.fixture
def repo(tmp_path):
    """An empty model repository rooted in a temp dir."""
    root = tmp_path / "models"
    root.mkdir()
    return root


@pytest.fixture
def make_model(repo):
    """Lay out one model directory: config + version dirs + artifact."""

    def _make(name, config, versions=("1",), artifact="model.onnx", model=None):
        model_dir = repo / name
        model_dir.mkdir()
        if config is not None:
            (model_dir / "config.pbtxt").write_text(config)
        for version in versions:
            (model_dir / version).mkdir()
            if model is not None:
                onnx.save(model, model_dir / version / artifact)
            else:
                (model_dir / version / artifact).write_bytes(b"not a model")
        return model_dir

    return _make


@pytest.fixture
def populated(make_model):
    """A repository shaped like the AF's: ONNX models that load, a TensorFlow
    one that does not."""
    make_model(
        "particleNetFromMiniAODAK8", PARTICLENET_CONFIG, model=particlenet_like_model()
    )
    make_model(
        "doubler",
        'name: "doubler"\nplatform: "onnxruntime_onnx"\nmax_batch_size: 0\n',
        model=doubler_model(),
    )
    make_model(
        "deepmet", DEEPMET_CONFIG, versions=("1", "2", "3"), artifact="model.graphdef"
    )


@pytest.fixture
def repository(populated, repo):
    from sonic_ray.repository import ModelRepository

    return ModelRepository(repo, providers=("CPUExecutionProvider",))
