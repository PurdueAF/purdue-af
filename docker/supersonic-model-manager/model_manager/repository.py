"""Filesystem layer for the model repository PVC.

Layout follows Triton's convention::

    <repository>/<model_name>/config.pbtxt
    <repository>/<model_name>/<version>/<model files>

Everything here is synchronous and blocking; callers run it in a thread.
"""

import os
import re
import shutil
import tarfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .config import settings
from .validation import validate_model_dir

STAGING_DIRNAME = ".uploads"
MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,255}$")
ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")


class RepositoryError(Exception):
    """User-facing error (bad upload, name clash, ...)."""


@dataclass
class ModelEntry:
    name: str
    size_bytes: int = 0
    file_count: int = 0
    versions: list = field(default_factory=list)
    has_config: bool = False
    modified: float = 0.0
    platform: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "sizeBytes": self.size_bytes,
            "fileCount": self.file_count,
            "versions": self.versions,
            "hasConfig": self.has_config,
            "modified": self.modified,
            "platform": self.platform,
        }


def repo_root() -> Path:
    return Path(settings.repository_path)


def _validate_name(name: str) -> str:
    name = (name or "").strip().strip("/")
    if not MODEL_NAME_RE.match(name) or name in (".", "..") or name.startswith("."):
        raise RepositoryError(
            f"Invalid model name {name!r}: use letters, digits, '.', '_' or '-' "
            "and do not start with a dot."
        )
    return name


def model_path(name: str) -> Path:
    """Resolve a model directory, guaranteeing it stays inside the repository."""
    name = _validate_name(name)
    root = repo_root().resolve()
    path = (root / name).resolve()
    if path.parent != root:
        raise RepositoryError(f"Refusing to access {name!r} outside the repository root.")
    return path


def _dir_stats(path: Path) -> tuple:
    """(total_bytes, file_count, newest_mtime) for a directory tree."""
    total = 0
    count = 0
    newest = 0.0
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
        for filename in filenames:
            fpath = os.path.join(dirpath, filename)
            try:
                st = os.lstat(fpath)
            except OSError:
                continue
            total += st.st_size
            count += 1
            newest = max(newest, st.st_mtime)
    return total, count, newest


def _read_platform(config_file: Path) -> str:
    """Best-effort 'platform'/'backend' from config.pbtxt (text protobuf)."""
    try:
        text = config_file.read_text(errors="replace")[:8192]
    except OSError:
        return ""
    match = re.search(r'^\s*(?:platform|backend)\s*:\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else ""


def scan_models() -> list:
    """List every model directory currently on the PVC."""
    root = repo_root()
    if not root.is_dir():
        return []

    entries = []
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir() or child.is_symlink():
            continue
        if child.name.startswith(".") or child.name == STAGING_DIRNAME:
            continue

        size, count, newest = _dir_stats(child)
        config_file = child / "config.pbtxt"
        versions = sorted(
            (sub.name for sub in child.iterdir() if sub.is_dir() and sub.name.isdigit()),
            key=int,
        )
        entries.append(
            ModelEntry(
                name=child.name,
                size_bytes=size,
                file_count=count,
                versions=versions,
                has_config=config_file.is_file(),
                modified=max(newest, child.stat().st_mtime),
                platform=_read_platform(config_file) if config_file.is_file() else "",
            )
        )
    return entries


def storage_usage(pvc_capacity_bytes=None) -> dict:
    """How full the claim is, and how much of that the models account for.

    The two are not the same when the repository is a subdirectory of a claim
    shared with other workloads (``af-shared-storage`` and friends): there,
    ``modelsBytes`` is a small slice of ``usedBytes``. Capacity prefers the
    PVC's reported capacity, because statvfs on CephFS/NFS reports the backing
    filesystem rather than the claim.
    """
    root = repo_root()
    models_bytes, _, _ = _dir_stats(root) if root.is_dir() else (0, 0, 0.0)

    fs_total = fs_free = None
    try:
        st = os.statvfs(root)
        fs_total = st.f_blocks * st.f_frsize
        fs_free = st.f_bavail * st.f_frsize
    except OSError:
        pass

    if pvc_capacity_bytes:
        total = pvc_capacity_bytes
        source = "pvc"
    elif fs_total:
        total = fs_total
        source = "statvfs"
    else:
        total = 0
        source = "unknown"

    # Everything on the claim, not just our models — this is what "how full is
    # the PVC" actually means. Falls back to the model tree when statvfs is
    # unavailable.
    if fs_total and fs_free is not None:
        used = max(fs_total - fs_free, models_bytes)
    else:
        used = models_bytes

    if fs_free is not None and total:
        free = min(fs_free, max(total - models_bytes, 0))
    elif total:
        free = max(total - used, 0)
    else:
        free = fs_free

    return {
        "usedBytes": used,
        "modelsBytes": models_bytes,
        "totalBytes": total,
        "freeBytes": free,
        "source": source,
        "filesystemTotalBytes": fs_total,
        "filesystemFreeBytes": fs_free,
        "path": str(root),
        "writable": os.access(root, os.W_OK) if root.is_dir() else False,
    }


# --------------------------------------------------------------------------
# Uploads
# --------------------------------------------------------------------------


def _staging_dir() -> Path:
    staging = repo_root() / STAGING_DIRNAME / f"stage-{int(time.time() * 1000)}-{os.getpid()}"
    staging.mkdir(parents=True, exist_ok=False)
    return staging


def _safe_relpath(member_name: str) -> Path:
    """Reject absolute paths, traversal and drive letters in archive members."""
    name = member_name.replace("\\", "/").lstrip("/")
    parts = []
    for part in name.split("/"):
        if part in ("", "."):
            continue
        if part == ".." or ":" in part:
            raise RepositoryError(f"Unsafe path in archive: {member_name!r}")
        parts.append(part)
    if not parts:
        raise RepositoryError(f"Unsafe path in archive: {member_name!r}")
    return Path(*parts)


def _is_junk(relative: Path) -> bool:
    """Archiver noise: macOS AppleDouble/resource forks, Finder and Windows files."""
    for part in relative.parts:
        if part == "__MACOSX" or part.startswith("._"):
            return True
        if part in (".DS_Store", "Thumbs.db", "desktop.ini"):
            return True
    return False


def _extract_zip(archive: Path, dest: Path, budget: int) -> None:
    with zipfile.ZipFile(archive) as zf:
        remaining = budget
        for info in zf.infolist():
            rel = _safe_relpath(info.filename)
            if _is_junk(rel):
                continue
            target = dest / rel
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            remaining -= info.file_size
            if remaining < 0:
                raise RepositoryError("Archive expands beyond the configured upload size limit.")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, 1024 * 1024)


def _extract_tar(archive: Path, dest: Path, budget: int) -> None:
    with tarfile.open(archive, "r:*") as tf:
        remaining = budget
        for member in tf:
            if member.name.startswith("PaxHeader") or "/PaxHeader" in member.name:
                continue
            if member.issym() or member.islnk():
                raise RepositoryError(f"Archive contains a link ({member.name!r}); refusing.")
            if not (member.isfile() or member.isdir()):
                raise RepositoryError(f"Unsupported archive entry type: {member.name!r}")
            rel = _safe_relpath(member.name)
            if _is_junk(rel):
                continue
            target = dest / rel
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            remaining -= member.size
            if remaining < 0:
                raise RepositoryError("Archive expands beyond the configured upload size limit.")
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(member)
            if src is None:
                continue
            with src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, 1024 * 1024)


def is_archive(filename: str) -> bool:
    lowered = (filename or "").lower()
    return lowered.endswith(ARCHIVE_SUFFIXES)


def _looks_like_model_dir(path: Path) -> bool:
    if (path / "config.pbtxt").is_file():
        return True
    return any(sub.is_dir() and sub.name.isdigit() for sub in path.iterdir())


def _resolve_model_root(staging: Path, max_depth: int = 5) -> Path:
    """Descend through wrapper directories to the actual model directory.

    Archives are often zipped from a parent folder ("release-2026/mymodel/..."),
    sometimes more than one level up. Descending stops as soon as a directory
    looks like a model, or as soon as there is more than one candidate — an
    archive holding several models is left alone so validation can reject it.
    """
    current = staging
    for _ in range(max_depth):
        if _looks_like_model_dir(current):
            return current
        children = [c for c in current.iterdir() if not _is_junk(Path(c.name))]
        if len(children) == 1 and children[0].is_dir():
            current = children[0]
            continue
        break
    return current


class ValidationFailed(RepositoryError):
    """The staged model does not satisfy Triton's layout rules."""

    def __init__(self, result):
        self.result = result
        super().__init__(" ".join(result.errors))


def _validate(source: Path, name: str):
    """Run Triton-compliance checks, raising on anything that would not load."""
    result = validate_model_dir(source, name)
    if not result.ok:
        raise ValidationFailed(result)
    return result


def _install(source: Path, name: str, overwrite: bool) -> dict:
    """Move a staged model tree into place, replacing atomically-ish."""
    target = model_path(name)
    if target.exists():
        if not overwrite:
            raise RepositoryError(
                f"Model {name!r} already exists. Re-upload with overwrite enabled to replace it."
            )
        backup = target.with_name(f".{target.name}.replaced-{int(time.time())}")
        target.rename(backup)
        try:
            shutil.move(str(source), str(target))
        except Exception:
            backup.rename(target)
            raise
        shutil.rmtree(backup, ignore_errors=True)
    else:
        shutil.move(str(source), str(target))

    size, count, _ = _dir_stats(target)
    return {"name": name, "sizeBytes": size, "fileCount": count}


def install_archive(archive_path: Path, original_filename: str, name: str, overwrite: bool) -> dict:
    """Unpack an uploaded archive into the repository as model ``name``."""
    staging = _staging_dir()
    try:
        extract_to = staging / "extract"
        extract_to.mkdir()
        if original_filename.lower().endswith(".zip"):
            _extract_zip(archive_path, extract_to, settings.max_upload_bytes)
        else:
            _extract_tar(archive_path, extract_to, settings.max_upload_bytes)

        model_root = _resolve_model_root(extract_to)
        if not name:
            name = model_root.name if model_root is not extract_to else _strip_archive_suffix(
                original_filename
            )
        name = _validate_name(name)

        validation = _validate(model_root, name)
        installed = _install(model_root, name, overwrite)
        installed["validation"] = validation.to_dict()
        return installed
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _strip_archive_suffix(filename: str) -> str:
    lowered = filename.lower()
    for suffix in sorted(ARCHIVE_SUFFIXES, key=len, reverse=True):
        if lowered.endswith(suffix):
            return filename[: -len(suffix)]
    return Path(filename).stem


class DirectoryUpload:
    """Collects individually-uploaded files (browser directory picker) on disk."""

    def __init__(self, name: str, overwrite: bool):
        self.name = _validate_name(name)
        self.overwrite = overwrite
        self.staging = _staging_dir()
        self.root = self.staging / "model"
        self.root.mkdir()
        self.written = 0

    def add_file(self, relative_path: str, stream) -> None:
        rel = _safe_relpath(relative_path)
        if _is_junk(rel):
            return
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "wb") as out:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                self.written += len(chunk)
                if self.written > settings.max_upload_bytes:
                    raise RepositoryError("Upload exceeds the configured size limit.")
                out.write(chunk)

    def finish(self) -> dict:
        try:
            model_root = _resolve_model_root(self.root)
            validation = _validate(model_root, self.name)
            installed = _install(model_root, self.name, self.overwrite)
            installed["validation"] = validation.to_dict()
            return installed
        finally:
            self.cleanup()

    def cleanup(self) -> None:
        shutil.rmtree(self.staging, ignore_errors=True)


def delete_model(name: str) -> None:
    target = model_path(name)
    if not target.is_dir():
        raise RepositoryError(f"Model {name!r} is not present on the PVC.")
    shutil.rmtree(target)


def cleanup_staging(max_age_s: int = 3600) -> None:
    """Drop staging dirs left behind by an interrupted upload."""
    staging_root = repo_root() / STAGING_DIRNAME
    if not staging_root.is_dir():
        return
    cutoff = time.time() - max_age_s
    for child in staging_root.iterdir():
        try:
            if child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            continue
