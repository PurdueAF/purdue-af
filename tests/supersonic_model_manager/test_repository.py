"""Model repository filesystem layer: scanning, usage, safe extraction."""

import io
import os
import tarfile
import zipfile
from pathlib import Path

import pytest
from model_manager import repository
from model_manager.config import settings


def build_zip(path, entries):
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return path


def build_tar(path, entries, links=None):
    with tarfile.open(path, "w:gz") as tf:
        for name, content in entries.items():
            data = content.encode() if isinstance(content, str) else content
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tf.addfile(info)
    return path


MODEL_FILES = {
    "mymodel/config.pbtxt": 'name: "mymodel"\nplatform: "onnxruntime_onnx"\n',
    "mymodel/1/model.onnx": "weights",
}


# --------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------


def test_scan_reports_size_versions_and_platform(repo, make_model):
    make_model("particlenet", versions=("1", "2"), size=64)

    entries = repository.scan_models()

    assert [e.name for e in entries] == ["particlenet"]
    entry = entries[0]
    assert entry.versions == ["1", "2"]
    assert entry.platform == "onnxruntime_onnx"
    assert entry.has_config is True
    assert entry.size_bytes == 64 * 2 + len(
        'name: "particlenet"\nplatform: "onnxruntime_onnx"\nmax_batch_size: 8\n'
    )


def test_scan_skips_hidden_and_staging_directories(repo, make_model):
    make_model("real")
    (repo / repository.STAGING_DIRNAME).mkdir()
    (repo / ".hidden").mkdir()

    assert [e.name for e in repository.scan_models()] == ["real"]


def test_scan_skips_files_and_symlinked_directories(repo, make_model, tmp_path):
    make_model("real")
    (repo / "README").write_text("not a model")
    elsewhere = make_model("outside", parent=tmp_path)
    (repo / "linked").symlink_to(elsewhere)

    assert [e.name for e in repository.scan_models()] == ["real"]


def test_scan_ignores_files_that_vanish_mid_walk(repo, make_model, monkeypatch):
    make_model("m", size=10)
    real_lstat = os.lstat

    def flaky_lstat(path, *args, **kwargs):
        if str(path).endswith("model.onnx"):
            raise FileNotFoundError(path)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", flaky_lstat)

    entry = repository.scan_models()[0]

    assert entry.file_count == 1, "only config.pbtxt was countable"


def test_unreadable_config_has_no_platform(tmp_path):
    assert repository._read_platform(tmp_path) == ""


def test_scan_of_missing_repository_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "repository_path", str(tmp_path / "absent"))

    assert repository.scan_models() == []


# --------------------------------------------------------------------------
# Usage accounting
# --------------------------------------------------------------------------


def test_usage_separates_claim_fullness_from_model_footprint(repo, make_model):
    make_model("m1", size=1024)

    usage = repository.storage_usage(pvc_capacity_bytes=10 * 1024**3)

    assert usage["totalBytes"] == 10 * 1024**3
    assert usage["source"] == "pvc"
    # models are a slice of everything on the claim
    assert usage["modelsBytes"] >= 1024
    assert usage["usedBytes"] >= usage["modelsBytes"]


def test_usage_never_reports_more_free_than_the_filesystem_has(repo, make_model):
    """A shared claim's free space is bounded by the filesystem, not the quota."""
    make_model("m1", size=1024)

    usage = repository.storage_usage(pvc_capacity_bytes=40 * 1024**4)  # 40 TiB claim

    assert usage["freeBytes"] <= usage["filesystemFreeBytes"]


def test_usage_falls_back_to_statvfs_without_pvc_capacity(repo):
    usage = repository.storage_usage(pvc_capacity_bytes=0)

    assert usage["source"] == "statvfs"
    assert usage["totalBytes"] > 0


def test_usage_without_statvfs_uses_the_pvc_capacity(repo, make_model, monkeypatch):
    make_model("m1", size=1000)

    def no_statvfs(path):
        raise OSError("unsupported")

    monkeypatch.setattr(os, "statvfs", no_statvfs)

    usage = repository.storage_usage(pvc_capacity_bytes=10_000)

    assert usage["source"] == "pvc"
    assert usage["usedBytes"] == usage["modelsBytes"]
    assert usage["freeBytes"] == 10_000 - usage["modelsBytes"]
    assert usage["filesystemTotalBytes"] is None


def test_usage_of_a_missing_repository_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "repository_path", str(tmp_path / "absent"))

    usage = repository.storage_usage()

    assert usage["source"] == "unknown"
    assert usage["totalBytes"] == 0
    assert usage["usedBytes"] == 0
    assert usage["freeBytes"] is None
    assert usage["writable"] is False


# --------------------------------------------------------------------------
# Archive extraction safety
# --------------------------------------------------------------------------


def test_installs_model_from_zip(repo, tmp_path):
    archive = build_zip(tmp_path / "m.zip", MODEL_FILES)

    result = repository.install_archive(archive, "m.zip", "", overwrite=False)

    assert result["name"] == "mymodel"
    assert (repo / "mymodel" / "1" / "model.onnx").is_file()
    assert result["validation"]["platform"] == "onnxruntime_onnx"


def test_installs_model_from_targz(repo, tmp_path):
    archive = build_tar(tmp_path / "m.tar.gz", MODEL_FILES)

    result = repository.install_archive(archive, "m.tar.gz", "", overwrite=False)

    assert result["name"] == "mymodel"
    assert (repo / "mymodel" / "1" / "model.onnx").is_file()


def test_strips_macos_appledouble_and_finder_noise(repo, tmp_path):
    """`tar` on macOS emits ._* members that otherwise break root detection."""
    entries = dict(MODEL_FILES)
    entries["._mymodel"] = "resource fork"
    entries["mymodel/._config.pbtxt"] = "resource fork"
    entries["mymodel/.DS_Store"] = "finder"
    entries["__MACOSX/mymodel/._1"] = "resource fork"
    archive = build_tar(tmp_path / "m.tar.gz", entries)

    result = repository.install_archive(archive, "m.tar.gz", "", overwrite=False)

    installed = repo / result["name"]
    assert not list(installed.rglob("._*"))
    assert not list(installed.rglob(".DS_Store"))
    assert (installed / "1" / "model.onnx").is_file()


def test_rejects_path_traversal_in_archive(repo, tmp_path):
    entries = dict(MODEL_FILES)
    entries["../../escaped.txt"] = "pwned"
    archive = build_zip(tmp_path / "evil.zip", entries)

    with pytest.raises(repository.RepositoryError, match="Unsafe path"):
        repository.install_archive(archive, "evil.zip", "evil", overwrite=False)

    assert not (repo.parent / "escaped.txt").exists()


def test_absolute_paths_are_contained_inside_the_model(repo, tmp_path):
    entries = {
        "config.pbtxt": 'name: "abs"\nplatform: "onnxruntime_onnx"\n',
        "1/model.onnx": "x",
        "/etc/passwd": "pwned",
    }
    archive = build_zip(tmp_path / "abs.zip", entries)

    repository.install_archive(archive, "abs.zip", "abs", overwrite=False)

    assert (repo / "abs" / "etc" / "passwd").is_file()
    assert Path("/etc/passwd").read_text() != "pwned"


def test_rejects_symlink_members(repo, tmp_path):
    archive = build_tar(
        tmp_path / "link.tar.gz",
        {"mymodel/config.pbtxt": 'name: "mymodel"\nplatform: "onnxruntime_onnx"\n'},
        links={"mymodel/1": "/etc"},
    )

    with pytest.raises(repository.RepositoryError, match="link"):
        repository.install_archive(archive, "link.tar.gz", "mymodel", overwrite=False)


@pytest.mark.parametrize(
    "build,filename", [(build_zip, "big.zip"), (build_tar, "big.tar.gz")]
)
def test_rejects_archive_larger_than_the_limit(
    repo, tmp_path, monkeypatch, build, filename
):
    monkeypatch.setattr(settings, "max_upload_bytes", 128)
    entries = dict(MODEL_FILES)
    entries["mymodel/1/model.onnx"] = "x" * 4096
    archive = build(tmp_path / filename, entries)

    with pytest.raises(repository.RepositoryError, match="size limit"):
        repository.install_archive(archive, filename, "mymodel", overwrite=False)


def test_directory_entries_and_junk_in_a_zip(repo, tmp_path):
    with zipfile.ZipFile(tmp_path / "d.zip", "w") as zf:
        zf.writestr("mymodel/", "")
        zf.writestr("mymodel/1/", "")
        zf.writestr("__MACOSX/mymodel/._config.pbtxt", "fork")
        for name, content in MODEL_FILES.items():
            zf.writestr(name, content)

    result = repository.install_archive(tmp_path / "d.zip", "d.zip", "", False)

    assert (repo / result["name"] / "1" / "model.onnx").is_file()
    assert not list(repo.rglob("._*"))


def test_directory_and_pax_entries_in_a_tar(repo, tmp_path):
    archive = tmp_path / "d.tar"
    with tarfile.open(archive, "w", format=tarfile.PAX_FORMAT) as tf:
        folder = tarfile.TarInfo("mymodel/1")
        folder.type = tarfile.DIRTYPE
        tf.addfile(folder)
        pax = tarfile.TarInfo("PaxHeader/mymodel")
        pax.size = 1
        tf.addfile(pax, io.BytesIO(b"x"))
        for name, content in MODEL_FILES.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.pax_headers = {"comment": "non-ascii é"}
            tf.addfile(info, io.BytesIO(content.encode()))

    result = repository.install_archive(archive, "d.tar", "", False)

    assert result["name"] == "mymodel"
    assert not list(repo.rglob("PaxHeader*"))


def test_rejects_special_files_in_a_tar(repo, tmp_path):
    archive = tmp_path / "fifo.tar"
    with tarfile.open(archive, "w") as tf:
        fifo = tarfile.TarInfo("mymodel/pipe")
        fifo.type = tarfile.FIFOTYPE
        tf.addfile(fifo)

    with pytest.raises(repository.RepositoryError, match="Unsupported"):
        repository.install_archive(archive, "fifo.tar", "mymodel", False)


@pytest.mark.parametrize("member", ["C:/evil", "./", "/"])
def test_rejects_drive_letters_and_empty_members(member):
    with pytest.raises(repository.RepositoryError, match="Unsafe path"):
        repository._safe_relpath(member)


def test_archive_holding_the_model_at_its_root_is_named_after_the_file(repo, tmp_path):
    archive = build_tar(
        tmp_path / "x.tgz",
        {
            "config.pbtxt": 'platform: "onnxruntime_onnx"\n',
            "1/model.onnx": "w",
        },
    )

    result = repository.install_archive(archive, "Tagger.v2.TGZ", "", False)

    assert result["name"] == "Tagger.v2"


@pytest.mark.parametrize(
    "filename,stem",
    [("m.tar.gz", "m"), ("m.tar", "m"), ("m.tar.bz2", "m"), ("model.bin", "model")],
)
def test_strip_archive_suffix(filename, stem):
    assert repository._strip_archive_suffix(filename) == stem


def test_archive_with_several_models_is_rejected(repo, tmp_path):
    entries = dict(MODEL_FILES)
    entries["other/config.pbtxt"] = 'name: "other"\nplatform: "onnxruntime_onnx"\n'
    entries["other/1/model.onnx"] = "w"
    archive = build_zip(tmp_path / "two.zip", entries)

    with pytest.raises(repository.ValidationFailed):
        repository.install_archive(archive, "two.zip", "both", False)

    assert not (repo / "both").exists()


def test_invalid_model_is_rejected_before_install(repo, tmp_path):
    entries = {
        "flat/config.pbtxt": 'name: "flat"\nplatform: "onnxruntime_onnx"\n',
        "flat/model.onnx": "x",  # no version directory
    }
    archive = build_zip(tmp_path / "flat.zip", entries)

    with pytest.raises(repository.ValidationFailed) as excinfo:
        repository.install_archive(archive, "flat.zip", "flat", overwrite=False)

    assert excinfo.value.result.errors
    assert not (repo / "flat").exists(), "a rejected model must not be installed"


def test_unwraps_archive_with_a_single_wrapping_directory(repo, tmp_path):
    entries = {
        "release-2026/mymodel/config.pbtxt": 'name: "mymodel"\nplatform: "onnxruntime_onnx"\n',
        "release-2026/mymodel/1/model.onnx": "x",
    }
    archive = build_zip(tmp_path / "wrapped.zip", entries)

    result = repository.install_archive(
        archive, "wrapped.zip", "mymodel", overwrite=False
    )

    assert (repo / "mymodel" / "1" / "model.onnx").is_file()
    assert result["name"] == "mymodel"


# --------------------------------------------------------------------------
# Overwrite, delete, naming
# --------------------------------------------------------------------------


def test_refuses_to_replace_existing_model_without_overwrite(
    repo, tmp_path, make_model
):
    make_model("mymodel")
    archive = build_zip(tmp_path / "m.zip", MODEL_FILES)

    with pytest.raises(repository.RepositoryError, match="already exists"):
        repository.install_archive(archive, "m.zip", "mymodel", overwrite=False)


def test_overwrite_replaces_and_leaves_no_backup(repo, tmp_path, make_model):
    make_model("mymodel", versions=("1", "7"))
    archive = build_zip(tmp_path / "m.zip", MODEL_FILES)

    repository.install_archive(archive, "m.zip", "mymodel", overwrite=True)

    assert (repo / "mymodel" / "1" / "model.onnx").read_text() == "weights"
    assert not (repo / "mymodel" / "7").exists()
    assert not list(repo.glob(".mymodel.replaced-*"))


def test_failed_overwrite_restores_the_previous_model(
    repo, tmp_path, make_model, monkeypatch
):
    make_model("mymodel", versions=("7",))
    archive = build_zip(tmp_path / "m.zip", MODEL_FILES)

    def broken_move(src, dst):
        raise OSError("quota exceeded")

    monkeypatch.setattr(repository.shutil, "move", broken_move)

    with pytest.raises(OSError, match="quota"):
        repository.install_archive(archive, "m.zip", "mymodel", overwrite=True)

    assert (repo / "mymodel" / "7" / "model.onnx").is_file()
    assert not list(repo.glob(".mymodel.replaced-*"))


@pytest.mark.parametrize("name", ["../escape", "..", ".hidden", "with/slash", ""])
def test_rejects_unsafe_model_names(repo, name):
    with pytest.raises(repository.RepositoryError):
        repository.model_path(name)


def test_refuses_a_model_symlinked_out_of_the_repository(repo, tmp_path, make_model):
    (repo / "escape").symlink_to(make_model("target", parent=tmp_path))

    with pytest.raises(repository.RepositoryError, match="outside"):
        repository.delete_model("escape")

    assert (tmp_path / "target").is_dir()


def test_delete_removes_model(repo, make_model):
    make_model("doomed")

    repository.delete_model("doomed")

    assert not (repo / "doomed").exists()


def test_delete_missing_model_raises(repo):
    with pytest.raises(repository.RepositoryError, match="not present"):
        repository.delete_model("ghost")


def test_directory_upload_drops_junk_and_installs(repo):
    upload = repository.DirectoryUpload("dirmodel", overwrite=False)
    upload.add_file(
        "config.pbtxt", io.BytesIO(b'name: "dirmodel"\nplatform: "onnxruntime_onnx"\n')
    )
    upload.add_file("1/model.onnx", io.BytesIO(b"x"))
    upload.add_file(".DS_Store", io.BytesIO(b"junk"))

    result = upload.finish()

    assert result["name"] == "dirmodel"
    assert (repo / "dirmodel" / "1" / "model.onnx").is_file()
    assert not (repo / "dirmodel" / ".DS_Store").exists()


def test_directory_upload_enforces_the_size_limit(repo, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_bytes", 4)
    upload = repository.DirectoryUpload("big", overwrite=False)

    with pytest.raises(repository.RepositoryError, match="size limit"):
        upload.add_file("1/model.onnx", io.BytesIO(b"x" * 16))

    upload.cleanup()
    assert not upload.staging.exists()


def test_cleanup_staging_without_a_staging_area_is_a_no_op(repo):
    repository.cleanup_staging()

    assert not (repo / repository.STAGING_DIRNAME).exists()


def test_cleanup_staging_keeps_fresh_and_skips_vanished_entries(repo):
    staging_root = repo / repository.STAGING_DIRNAME
    fresh = staging_root / "stage-new"
    fresh.mkdir(parents=True)
    (staging_root / "dangling").symlink_to(staging_root / "gone")

    repository.cleanup_staging(max_age_s=60)

    assert fresh.exists()


def test_cleanup_staging_removes_stale_directories(repo):
    staging = repo / repository.STAGING_DIRNAME / "stage-old"
    staging.mkdir(parents=True)
    old = 1_000_000
    os.utime(staging, (old, old))

    repository.cleanup_staging(max_age_s=60)

    assert not staging.exists()
