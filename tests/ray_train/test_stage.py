"""Tests for apps/ray-train/stage.py, with a pyarrow.fs that records what it is asked."""

import sys
import types

import pytest
from common import REPO, load_script


class Recorded:
    LocalFileSystem = staticmethod(lambda: "local")

    class FileType:
        NotFound = "missing"
        Directory = "dir"

    def __init__(self, present):
        self.present = present
        self.copies = []

    def S3FileSystem(self, **options):
        assert options == {
            "endpoint_override": "s3.example",
            "scheme": "https",
            "region": "us-east-1",
        }
        return self

    def get_file_info(self, path):
        return types.SimpleNamespace(type="dir" if self.present else "missing")

    def copy_files(self, source, target, source_filesystem, destination_filesystem):
        self.copies.append((source, target, source_filesystem, destination_filesystem))


@pytest.fixture
def stage(monkeypatch):
    def load(present=True):
        recorded = Recorded(present)
        pyarrow = types.ModuleType("pyarrow")
        pyarrow.fs = recorded
        monkeypatch.setitem(sys.modules, "pyarrow", pyarrow)
        monkeypatch.setitem(sys.modules, "pyarrow.fs", recorded)
        monkeypatch.setenv("AWS_ENDPOINT_URL", "https://s3.example")
        module = load_script(REPO / "apps" / "ray-train" / "stage.py", "stage")
        return module, recorded

    return load


def test_inputs_go_from_af_storage_to_the_jobs_prefix(stage):
    module, recorded = stage()
    module.main("in", "/work/users/a/skims", "s3://bucket/ray-nrp/jobs/j/inputs")
    assert recorded.copies == [
        ("/work/users/a/skims", "bucket/ray-nrp/jobs/j/inputs", "local", recorded)
    ]


def test_outputs_come_back_into_a_directory_made_for_them(stage, tmp_path):
    module, recorded = stage()
    target = tmp_path / "runs" / "new"
    module.main("out", "s3://bucket/ray-nrp/jobs/j/output", str(target))
    assert target.is_dir()
    assert recorded.copies == [
        ("bucket/ray-nrp/jobs/j/output", str(target), recorded, "local")
    ]


def test_a_job_that_wrote_nothing_has_nothing_copied_back(stage, tmp_path, capsys):
    module, recorded = stage(present=False)
    module.main("out", "s3://bucket/ray-nrp/jobs/j/output", str(tmp_path / "runs"))
    assert recorded.copies == []
    assert "nothing to copy" in capsys.readouterr().out
