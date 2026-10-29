"""Copies a job on NRP's inputs from AF storage to its S3 prefix, or its outputs
back: `python stage.py in <path> <s3 uri>` or `python stage.py out <s3 uri> <path>`.
The gateway runs it as the job's owner, in a Job of its own (gateway.py)."""

import os
import sys
from urllib.parse import urlparse

import pyarrow.fs as fs


def bucket() -> fs.S3FileSystem:
    endpoint = urlparse(os.environ["AWS_ENDPOINT_URL"])
    # Ceph ignores the region; naming one keeps pyarrow from asking for it.
    return fs.S3FileSystem(
        endpoint_override=endpoint.netloc, scheme=endpoint.scheme, region="us-east-1"
    )


def make_directory(path: str) -> None:
    """Makes `path` and its missing parents, none of them at the top of a mounted volume."""
    top = path
    while not os.path.isdir(os.path.dirname(top)):
        top = os.path.dirname(top)
    if not os.path.isdir(top) and os.path.ismount(os.path.dirname(top)):
        raise SystemExit(f"{top} would be made at the top of a shared volume")
    os.makedirs(path, exist_ok=True)


def main(direction: str, source: str, target: str) -> None:
    local, s3 = fs.LocalFileSystem(), bucket()
    if direction == "in":
        fs.copy_files(
            source,
            target.removeprefix("s3://"),
            source_filesystem=local,
            destination_filesystem=s3,
        )
    else:
        source = source.removeprefix("s3://")
        if s3.get_file_info(source).type == fs.FileType.NotFound:
            print(f"{source} has nothing to copy", flush=True)
            return
        make_directory(target)
        fs.copy_files(
            source, target, source_filesystem=s3, destination_filesystem=local
        )
    print(f"copied {source} to {target}", flush=True)


if __name__ == "__main__":
    main(*sys.argv[1:])
