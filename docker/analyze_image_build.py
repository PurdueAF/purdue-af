#!/usr/bin/env python3
"""Where the time and bytes of a kaniko image build go.

Run locally after (or during) a kaniko build Job — the Job itself is not
modified in any way. Three views:

  1. build TIME per Dockerfile instruction, parsed from the kaniko pod log
     (kaniko timestamps every line by default), including snapshot overhead
     and cache hits;
  2. image SIZE per layer, by joining the registry manifest (layer sizes)
     with the image config history (instruction per layer);
  3. a directory-level breakdown of the largest layers, obtained by
     streaming each blob through `tarfile` — nothing is written to disk.

Usage:
  # full report: timing from the job log + sizes from the registry
  kubectl logs job/kaniko-build-af | \\
      python3 analyze_image_build.py --log - \\
          --image geddes-registry.rcac.purdue.edu/cms/purdue-af:0.12.5

  # size-only (no log needed, works for any pushed image)
  python3 analyze_image_build.py \\
      --image geddes-registry.rcac.purdue.edu/cms/purdue-af:0.12.5

Registry auth comes from ~/.docker/config.json (`docker login` first if the
repository is private). Stdlib only — no dependencies to install.

NOTE on caching: steps rebuilt from kaniko's `--cache` show near-zero time;
for a true timing baseline, run the job once with `--cache=false`.
"""

import argparse
import base64
import gzip  # noqa: F401  (tarfile "r|*" uses it transparently)
import json
import os
import re
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime

DOCKERFILE_INSTRUCTIONS = (
    "FROM",
    "RUN",
    "COPY",
    "ADD",
    "ENV",
    "ARG",
    "LABEL",
    "USER",
    "WORKDIR",
    "SHELL",
    "EXPOSE",
    "ENTRYPOINT",
    "CMD",
    "HEALTHCHECK",
    "VOLUME",
    "STOPSIGNAL",
    "ONBUILD",
)
_INSTRUCTION_RE = re.compile(r"^(%s)\b" % "|".join(DOCKERFILE_INSTRUCTIONS))
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_TEXT_KV_RE = re.compile(r'time="([^"]+)"\s+level=\w+\s+msg="((?:[^"\\]|\\.)*)"')
_BRACKET_RE = re.compile(r"^(?:DEBU|INFO|WARN|ERRO)\[(\d+)\]\s?(.*)$")
_FROM_AS_RE = re.compile(r"^FROM\s+\S+\s+(?:AS|as)\s+(\S+)")
# kaniko does not echo FROM lines; stages are announced by these two markers
_RESOLVED_STAGE_RE = re.compile(r"^Resolved base name \S+ to (\S+)")
_STAGE_START_RE = re.compile(r"^Executing \d+ build triggers")
# cross-stage bookkeeping kaniko does between instructions (storing/unpacking
# the stage filesystem tar) — real time, but attributable to no instruction
_BOUNDARY_RE = re.compile(
    r"^(Storing source image|Deleting filesystem|Unpacking rootfs"
    r"|Base image from previous stage|Saving file .+ for later use)"
)
# with --cache=true kaniko pushes cache layers mid-build using the same
# "Pushing image to <repo>/cache:<key>" wording as the final push
_CACHE_PUSH_RE = re.compile(r"/cache:[0-9a-f]{40,}\s*$")

MANIFEST_ACCEPT = ", ".join(
    [
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.index.v1+json",
    ]
)


# ---------------------------------------------------------------- log parsing


def _parse_time(value):
    """RFC3339 timestamp → epoch seconds (handles trailing Z)."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def parse_line(line):
    """One kaniko log line → (seconds, message) or (None, None).

    Accepts the three logrus renderings: the default `INFO[0042] msg` form
    with relative seconds (ANSI color codes stripped — this is what
    `kubectl logs` of a kaniko pod produces), key-value text with absolute
    timestamps, and --log-format=json. Only differences are used, so mixing
    absolute/relative within one file is the only unsupported case.
    """
    line = _ANSI_RE.sub("", line).strip()
    if not line:
        return None, None
    if line.startswith("{"):
        try:
            rec = json.loads(line)
            return _parse_time(rec["time"]), rec.get("msg", "")
        except (ValueError, KeyError):
            return None, None
    m = _TEXT_KV_RE.search(line)
    if m:
        msg = m.group(2).encode().decode("unicode_escape")
        return _parse_time(m.group(1)), msg
    m = _BRACKET_RE.match(line)
    if m:
        return float(m.group(1)), m.group(2)
    return None, None


def parse_kaniko_log(lines):
    """Kaniko log → timing summary.

    Returns {"setup_s", "overhead_s", "push_s", "total_s", "steps": [
        {"stage", "instruction", "start_s", "duration_s",
         "snapshot_s", "cached"}, ...]}.

    A step spans from its instruction echo to the next instruction echo,
    ending early at a cross-stage boundary (kaniko storing/unpacking the
    stage filesystem tar); the boundary→next-instruction gaps accumulate
    into `overhead_s`. Snapshot time is measured from the last "Taking
    snapshot" line inside the step to the step end. Cache-layer pushes
    (`…/cache:<key>`) are ignored; the push phase starts at the first
    push of a non-cache ref.
    """
    events = []
    for raw in lines:
        t, msg = parse_line(raw)
        if t is not None:
            events.append((t, msg))
    if not events:
        return {
            "setup_s": 0.0,
            "overhead_s": 0.0,
            "push_s": 0.0,
            "total_s": 0.0,
            "steps": [],
        }

    t0, t_last = events[0][0], events[-1][0]
    stage_names = []
    steps = []
    stage_idx = -1
    push_start = None
    overhead = 0.0
    boundary_start = None  # earliest boundary since the last step ended
    for t, msg in events:
        if push_start is not None:
            break
        m = _RESOLVED_STAGE_RE.match(msg)
        if m:
            stage_names.append(m.group(1))
            continue
        if _STAGE_START_RE.match(msg):
            stage_idx += 1
            continue
        if msg.startswith("Pushing image to") and not _CACHE_PUSH_RE.search(msg):
            push_start = t
            continue
        if not _INSTRUCTION_RE.match(msg):
            if steps and _BOUNDARY_RE.match(msg):
                steps[-1].setdefault("_end", t)
                if boundary_start is None:
                    boundary_start = t
            elif steps and "_end" not in steps[-1]:
                if msg.startswith("Taking snapshot"):
                    steps[-1]["_snap_start"] = t
                elif msg.startswith("Using caching version") or msg.startswith(
                    "Found cached layer"
                ):
                    steps[-1]["cached"] = True
            continue
        # a Dockerfile instruction begins
        if steps:
            steps[-1].setdefault("_end", t)
        if boundary_start is not None:
            overhead += t - boundary_start
            boundary_start = None
        if msg.startswith("FROM"):  # not emitted by kaniko; kept for other builders
            stage_idx += 1
            m = _FROM_AS_RE.match(msg)
            if m:
                stage_names.insert(stage_idx, m.group(1))
        stage = (
            stage_names[stage_idx]
            if 0 <= stage_idx < len(stage_names)
            else (f"stage-{stage_idx}" if stage_idx >= 0 else None)
        )
        steps.append(
            {
                "stage": stage,
                "instruction": msg,
                "start_s": t,
                "cached": False,
                "_snap_start": None,
            }
        )

    for step in steps:
        end = step.pop("_end", push_start or t_last)
        step["duration_s"] = max(0.0, end - step["start_s"])
        snap = step.pop("_snap_start", None)
        step["snapshot_s"] = max(0.0, end - snap) if snap is not None else 0.0
        step["start_s"] -= t0

    return {
        "setup_s": (steps[0]["start_s"] if steps else t_last - t0),
        "overhead_s": overhead,
        "push_s": (t_last - push_start) if push_start is not None else 0.0,
        "total_s": t_last - t0,
        "steps": steps,
    }


# ------------------------------------------------------------ registry client


def parse_image_ref(ref):
    """'host/repo/name:tag' → (registry, repository, tag)."""
    host, _, rest = ref.partition("/")
    if "/" not in ref or ("." not in host and ":" not in host):
        raise ValueError(f"image ref must include a registry host: {ref!r}")
    if ":" in rest.rsplit("/", 1)[-1]:
        repo, _, tag = rest.rpartition(":")
    else:
        repo, tag = rest, "latest"
    return host, repo, tag


def load_auth_header(config_dir, registry):
    """Basic auth header for `registry` from a docker config.json, or None."""
    for name in ("config.json", ".dockerconfigjson"):
        path = os.path.join(config_dir, name)
        if not os.path.exists(path):
            continue
        with open(path) as fh:
            auths = json.load(fh).get("auths", {})
        for key, entry in auths.items():
            if (
                urllib.parse.urlparse(key).netloc not in (registry, "")
                and key != registry
            ):
                continue
            if entry.get("auth"):
                return "Basic " + entry["auth"]
            if entry.get("username"):
                creds = f"{entry['username']}:{entry.get('password', '')}"
                return "Basic " + base64.b64encode(creds.encode()).decode()
    return None


class _AuthStrippingRedirect(urllib.request.HTTPRedirectHandler):
    """Drop Authorization when a registry redirects blobs to another host
    (e.g. object storage) — forwarding it there breaks the request."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        old_host = urllib.parse.urlparse(req.full_url).netloc
        if new is not None and urllib.parse.urlparse(newurl).netloc != old_host:
            new.headers.pop("Authorization", None)
        return new


class RegistryClient:
    """Minimal v2 registry client: basic auth with bearer-token fallback."""

    def __init__(self, registry, repository, auth_header=None):
        self.base = f"https://{registry}/v2/{repository}"
        self.auth_header = auth_header
        self._token = None
        self._opener = urllib.request.build_opener(_AuthStrippingRedirect())

    def _open(self, url, accept):
        headers = {"Accept": accept}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        elif self.auth_header:
            headers["Authorization"] = self.auth_header
        req = urllib.request.Request(url, headers=headers)
        return self._opener.open(req, timeout=120)  # noqa: S310 (https)

    def open(self, url, accept="*/*"):
        try:
            return self._open(url, accept)
        except urllib.error.HTTPError as err:
            if err.code != 401 or self._token:
                raise
            self._token = self._fetch_token(err.headers.get("WWW-Authenticate", ""))
            return self._open(url, accept)

    def _fetch_token(self, challenge):
        fields = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        if "realm" not in fields:
            raise RuntimeError(f"unsupported auth challenge: {challenge!r}")
        params = {k: v for k, v in fields.items() if k in ("service", "scope")}
        url = fields["realm"] + "?" + urllib.parse.urlencode(params)
        headers = {"Authorization": self.auth_header} if self.auth_header else {}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
            body = json.load(resp)
        return body.get("token") or body.get("access_token")

    def get_json(self, url, accept="*/*"):
        with self.open(url, accept) as resp:
            return json.load(resp)

    def manifest(self, tag, arch="amd64"):
        doc = self.get_json(f"{self.base}/manifests/{tag}", MANIFEST_ACCEPT)
        if "manifests" in doc:  # index / manifest list → pick the platform
            for entry in doc["manifests"]:
                platform = entry.get("platform", {})
                if platform.get("architecture") == arch:
                    return self.get_json(
                        f"{self.base}/manifests/{entry['digest']}", MANIFEST_ACCEPT
                    )
            raise RuntimeError(f"no {arch} manifest in index for {tag}")
        return doc

    def config(self, manifest):
        return self.get_json(f"{self.base}/blobs/{manifest['config']['digest']}")

    def open_blob(self, digest):
        return self.open(f"{self.base}/blobs/{digest}")


def join_layers_history(history, layers):
    """Config history + manifest layers → one row per non-empty layer."""
    rows = []
    layer_iter = iter(layers)
    for entry in history:
        if entry.get("empty_layer"):
            continue
        layer = next(layer_iter, None)
        if layer is None:
            break
        rows.append(
            {
                # base-image layers typically carry no created_by
                "created_by": entry.get("created_by") or "(base image layer)",
                "size": layer["size"],
                "digest": layer["digest"],
            }
        )
    return rows


def aggregate_tar_dirs(fileobj, depth=3, top=20):
    """Stream a (compressed) layer tar → [(dir, uncompressed_bytes)], largest
    first. Reads headers and skips file data block-by-block: O(1) memory."""
    sizes = Counter()
    with tarfile.open(fileobj=fileobj, mode="r|*") as tar:
        for member in tar:
            if member.isfile():
                parts = member.name.lstrip("./").split("/")
                sizes["/".join(parts[:depth])] += member.size
    return sizes.most_common(top)


# ------------------------------------------------------------------ rendering


def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n:.0f} B"
        n /= 1024


def human_duration(s):
    if s < 60:
        return f"{s:.0f}s"
    if s < 3600:
        return f"{int(s // 60)}m{int(s % 60):02d}s"
    return f"{int(s // 3600)}h{int(s % 3600 // 60):02d}m"


def _truncate(text, width=88):
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


def render_time_report(timing):
    lines = [
        "",
        "=" * 100,
        "BUILD TIME BY DOCKERFILE INSTRUCTION (slowest first)",
        "=" * 100,
    ]
    lines.append(
        f"{'time':>8}  {'snapshot':>8}  {'cache':>5}  {'stage':<8} instruction"
    )
    for step in sorted(timing["steps"], key=lambda s: -s["duration_s"]):
        lines.append(
            f"{human_duration(step['duration_s']):>8}  "
            f"{human_duration(step['snapshot_s']):>8}  "
            f"{'HIT' if step['cached'] else '':>5}  "
            f"{(step['stage'] or '?'):<8} {_truncate(step['instruction'])}"
        )
    per_stage = Counter()
    for step in timing["steps"]:
        per_stage[step["stage"] or "?"] += step["duration_s"]
    lines.append("-" * 100)
    for stage, secs in per_stage.most_common():
        lines.append(f"{human_duration(secs):>8}  stage {stage}")
    lines.append(f"{human_duration(timing['setup_s']):>8}  context/rootfs setup")
    lines.append(
        f"{human_duration(timing.get('overhead_s', 0)):>8}  "
        "cross-stage store/unpack (kaniko bookkeeping)"
    )
    lines.append(f"{human_duration(timing['push_s']):>8}  push")
    lines.append(f"{human_duration(timing['total_s']):>8}  TOTAL")
    return "\n".join(lines)


def render_size_report(rows):
    total = sum(r["size"] for r in rows) or 1
    lines = [
        "",
        "=" * 100,
        "IMAGE SIZE BY LAYER (compressed, largest first)",
        "=" * 100,
    ]
    lines.append(f"{'size':>10}  {'%':>5}  instruction")
    for row in sorted(rows, key=lambda r: -r["size"]):
        lines.append(
            f"{human_size(row['size']):>10}  {100 * row['size'] / total:>4.1f}%  "
            f"{_truncate(row['created_by'])}"
        )
    lines.append("-" * 100)
    lines.append(f"{human_size(total):>10}  TOTAL (compressed; pull cost)")
    return "\n".join(lines)


def render_deep_dive(digest, created_by, dirs):
    lines = [
        "",
        "-" * 100,
        f"INSIDE {digest[:19]}…  ({_truncate(created_by, 70)})",
        f"{'uncompressed':>13}  directory",
    ]
    for path, size in dirs:
        lines.append(f"{human_size(size):>13}  {path}")
    return "\n".join(lines)


# ----------------------------------------------------------------------- main


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Time/size breakdown of a kaniko image build.",
        epilog="Example: kubectl logs job/kaniko-build-af | %(prog)s --log - "
        "--image geddes-registry.rcac.purdue.edu/cms/purdue-af:0.12.5",
    )
    parser.add_argument(
        "--log",
        help="kaniko log file, or '-' for stdin (e.g. piped from kubectl logs); "
        "omit to skip the timing section",
    )
    parser.add_argument(
        "--image",
        help="pushed image ref for the size sections; omit to skip them",
    )
    parser.add_argument(
        "--docker-config",
        default=os.environ.get("DOCKER_CONFIG", os.path.expanduser("~/.docker")),
        help="dir with config.json for registry auth [%(default)s]",
    )
    parser.add_argument(
        "--deep-dive-min-mb",
        type=float,
        default=300,
        help="stream and break down layers at least this large [%(default)s]",
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=3,
        help="path depth for the directory breakdown [%(default)s]",
    )
    parser.add_argument("--json", help="also write the report as JSON to this file")
    return parser


def run(args):
    report = {"image": args.image}

    if args.log:
        if args.log == "-":
            timing = parse_kaniko_log(sys.stdin)
        else:
            with open(args.log, errors="replace") as fh:
                timing = parse_kaniko_log(fh)
        if not timing["steps"]:
            print("WARNING: no Dockerfile instructions found in the log")
        report["timing"] = timing
        print(render_time_report(timing))

    if args.image:
        registry, repo, tag = parse_image_ref(args.image)
        auth = load_auth_header(args.docker_config, registry)
        client = RegistryClient(registry, repo, auth)
        manifest = client.manifest(tag)
        rows = join_layers_history(
            client.config(manifest).get("history", []), manifest.get("layers", [])
        )
        report["layers"] = rows
        print(render_size_report(rows))

        report["deep_dives"] = {}
        threshold = args.deep_dive_min_mb * 1024 * 1024
        for row in sorted(rows, key=lambda r: -r["size"]):
            if row["size"] < threshold:
                break
            with client.open_blob(row["digest"]) as blob:
                dirs = aggregate_tar_dirs(blob, depth=args.depth)
            report["deep_dives"][row["digest"]] = dirs
            print(render_deep_dive(row["digest"], row["created_by"], dirs))

    return report


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    if not args.log and not args.image:
        build_arg_parser().error("nothing to do: pass --log and/or --image")
    report = run(args)
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(report, fh, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
