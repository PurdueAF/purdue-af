#!/usr/bin/env python3
"""Post-install smoke test for a pixi environment: import every direct
dependency, each in its OWN fresh interpreter.

Run right after `pixi install` (no GPU needed, no computations — imports
only). Purpose: catch broken installs early, e.g. the pip-installed
tensorflow whose import died with `GLIBCXX_3.4.29 not found` because the
system libstdc++ shadowed the env's.

Design notes:
- pixi.toml is the single source of truth: [dependencies] +
  [pypi-dependencies] decide WHAT is checked; nothing is hard-coded, so env
  composition can change freely.
- Import names are derived mechanically (package name != module name, e.g.
  pytorch -> torch, root -> ROOT): conda packages from the env's
  conda-meta/*.json file lists, pypi packages from importlib.metadata.
  Packages that install no python modules (gsl, cmake, ...) are reported
  as "nopy" and don't fail the check.
- One fresh subprocess per package, because import failures can be
  LOAD-ORDER dependent: the GLIBCXX bug only triggered when tensorflow was
  imported first — a single-process import-all would mask it.

Usage (with the ENV's python, so metadata resolves against it):

    cd /work/pixi/global
    pixi run python /path/to/pixi/check-env.py            # or:
    .pixi/envs/default/bin/python /path/to/pixi/check-env.py

    check-env.py --manifest /work/pixi/global/pixi.toml --env default -j 8

Exit code: 0 if every derived import succeeded, 1 otherwise.
"""

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# top-level artifacts that are not real importable APIs
_SKIP_TOPLEVEL = {"tests", "test", "docs", "examples", "__pycache__", "benchmarks"}
_SP_RE = re.compile(
    r"(?:^|/)site-packages/([A-Za-z_][\w\-]*)(/__init__\.py|\.py|\.[\w-]+\.so)$"
)


def parse_manifest(path):
    """pixi.toml → (conda_deps, pypi_deps). Uses tomllib when available,
    else a line parser good enough for flat dependency tables."""
    text = path.read_text()
    try:
        import tomllib

        doc = tomllib.loads(text)
        conda = list(doc.get("dependencies", {}))
        pypi = list(doc.get("pypi-dependencies", {}))
        return conda, pypi
    except ModuleNotFoundError:  # python < 3.11
        conda, pypi, section = [], [], None
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            if line.startswith("["):
                section = line.strip("[]")
                continue
            m = re.match(r"^([\w.\-]+)\s*=", line)
            if not m:
                continue
            if section == "dependencies":
                conda.append(m.group(1))
            elif section == "pypi-dependencies":
                pypi.append(m.group(1))
        return conda, pypi


def conda_toplevels(env_dir):
    """conda-meta/*.json → {package name: sorted importable top-levels}.

    Metapackages (matplotlib, dask, jupyter, ...) install no files
    themselves — their code lives in dependencies (matplotlib-base,
    dask-core, ...). For packages with no own top-levels, follow ONE level
    of conda `depends` so their imports are still smoke-tested."""
    records = {}
    for meta in (env_dir / "conda-meta").glob("*.json"):
        try:
            rec = json.loads(meta.read_text())
        except ValueError:
            continue
        tops = set()
        for f in rec.get("files", []):
            m = _SP_RE.search(f)
            if m and not f.endswith(".pth"):
                name = m.group(1)
                if not name.startswith("_") and name not in _SKIP_TOPLEVEL:
                    tops.add(name)
        records[rec.get("name", meta.stem)] = (tops, rec.get("depends", []))

    result = {}
    for name, (tops, depends) in records.items():
        if not tops:  # metapackage: union the direct deps' own top-levels
            for dep in depends:
                dep_name = dep.split()[0]
                if dep_name in records:
                    tops |= records[dep_name][0]
        result[name] = sorted(tops)
    return result


def pypi_toplevels(name):
    """Installed-dist metadata → importable top-levels (run with env python)."""
    import importlib.metadata as md

    norm = re.sub(r"[-_.]+", "-", name).lower()
    for dist in md.distributions():
        dist_name = dist.metadata.get("Name") or ""
        if re.sub(r"[-_.]+", "-", dist_name).lower() != norm:
            continue
        tl = dist.read_text("top_level.txt")
        if tl:
            tops = [t.strip() for t in tl.splitlines() if t.strip()]
        else:
            tops = {
                str(f).split("/", 1)[0].removesuffix(".py")
                for f in (dist.files or [])
                if str(f).endswith((".py", ".so"))
                and "/" not in str(f).rstrip("/")
                or str(f).endswith("/__init__.py")
                and str(f).count("/") == 1
            }
        return sorted(
            t
            for t in tops
            if not t.startswith("_") and t not in _SKIP_TOPLEVEL and "." not in t
        )
    return None  # not installed at all


def import_check(python, modules, timeout):
    """Import `modules` in ONE fresh interpreter; → (ok, seconds, detail).

    Each subprocess gets a private HOME and TMPDIR: parallel imports of
    runtime-compiling packages (ROOT/cling in particular) race on shared
    cache/temp files and crash spuriously otherwise."""
    code = "; ".join(f"import {m}" for m in modules)
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="check-env-") as scratch:
        env = dict(
            os.environ,
            MPLBACKEND="Agg",
            TF_CPP_MIN_LOG_LEVEL="2",
            HOME=scratch,
            TMPDIR=scratch,
        )
        try:
            proc = subprocess.run(
                [python, "-c", code],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return False, timeout, f"import hung for {timeout:.0f}s"
    elapsed = time.monotonic() - start
    if proc.returncode == 0:
        return True, elapsed, ""
    lines = (proc.stdout + proc.stderr).strip().splitlines()
    # crash backtraces (e.g. cling's) put the informative frames well above
    # the end — keep a generous tail so CI logs are actionable
    detail = "\n        ".join(lines[-30:]) if lines else f"exit {proc.returncode}"
    if "GLIBCXX" in proc.stdout + proc.stderr:
        detail += "  [hint: system libstdc++ shadows the env's — see check-gpu.py]"
    return False, elapsed, detail


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("pixi.toml"),
        help="pixi.toml to read deps from [%(default)s]",
    )
    parser.add_argument("--env", default="default", help="env name [%(default)s]")
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=min(8, os.cpu_count() or 1),
        help="parallel import processes [%(default)s]",
    )
    parser.add_argument(
        "--timeout", type=float, default=180, help="seconds per package [%(default)s]"
    )
    args = parser.parse_args()

    manifest = args.manifest.resolve()
    if not manifest.exists():
        sys.exit(f"no {manifest} — run from the env directory or pass --manifest")
    env_dir = manifest.parent / ".pixi" / "envs" / args.env
    python = env_dir / "bin" / "python"
    if not python.exists():
        sys.exit(f"no interpreter at {python} — has `pixi install` run?")

    conda_deps, pypi_deps = parse_manifest(manifest)
    meta = conda_toplevels(env_dir)

    plan = {}  # package -> modules to import (None → not resolvable)
    for dep in conda_deps:
        plan[dep] = meta.get(dep)
    resolver = subprocess.run(  # resolve pypi tops with the ENV python
        [str(python), __file__, "--_resolve-pypi", *pypi_deps],
        capture_output=True,
        text=True,
    )
    plan.update(json.loads(resolver.stdout or "{}"))

    checks = {p: m for p, m in plan.items() if m}
    nopy = sorted(p for p, m in plan.items() if m is not None and not m)
    missing = sorted(p for p, m in plan.items() if m is None)

    print(f"manifest: {manifest}\nenv python: {python}")
    print(
        f"packages: {len(plan)} declared | {len(checks)} importable |"
        f" {len(nopy)} no-python | {len(missing)} NOT INSTALLED\n"
    )
    failed = list(missing)
    with concurrent.futures.ThreadPoolExecutor(args.jobs) as pool:
        futures = {
            pool.submit(import_check, str(python), mods, args.timeout): pkg
            for pkg, mods in sorted(checks.items())
        }
        for fut in concurrent.futures.as_completed(futures):
            pkg = futures[fut]
            ok, elapsed, detail = fut.result()
            status = "PASS" if ok else "FAIL"
            extra = f"({', '.join(checks[pkg])})" if ok else detail
            print(f"{status:<5} {pkg:<24} {elapsed:5.1f}s  {extra}")
            if not ok:
                failed.append(pkg)

    for pkg in missing:
        print(f"FAIL  {pkg:<24}   n/a   not installed in the env")
    if nopy:
        print(f"\nno python modules (skipped): {', '.join(nopy)}")
    print()
    if failed:
        print(f"FAILURES: {', '.join(sorted(failed))}")
        return 1
    print("all imports OK")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--_resolve-pypi":
        # internal mode, executed with the env's python
        print(json.dumps({name: pypi_toplevels(name) for name in sys.argv[2:]}))
        sys.exit(0)
    sys.exit(main())
