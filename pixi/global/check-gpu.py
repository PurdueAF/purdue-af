#!/usr/bin/env python3
"""Quick CUDA sanity check for every GPU-enabled framework in the global
pixi env (pixi/global/pixi.toml): torch (+torch-geometric), xgboost,
numba.cuda, and ROOT's RooFit CUDA backend.

Each framework runs a REAL small GPU computation (not just is_available())
in its own subprocess, so a segfault, driver hang, or CUDA-context problem
in one framework cannot take down the others — and a hang is cut off by the
per-check timeout. Frameworks that ride on these (zuko, dask-xgboost) are
covered transitively.

Run inside a GPU session, with the global env's python on PATH:

    python3 check-gpu.py              # all checks
    python3 check-gpu.py --only torch,xgboost
    python3 check-gpu.py --timeout 300

Exit code: 0 if nothing failed (SKIP is not a failure), 1 otherwise.
"""

import argparse
import shutil
import subprocess
import sys
import time

SKIP_EXIT = 42  # checks exit with this (+ a printed reason) for "not a failure"

CHECKS: dict[str, str] = {
    "torch": """
import torch
assert torch.cuda.is_available(), "torch.cuda.is_available() is False"
dev = torch.device("cuda:0")
a = torch.randn(512, 512, device=dev)
(a @ a).sum().item()                       # cuBLAS
import torch.nn.functional as F
x = torch.randn(1, 3, 32, 32, device=dev)
w = torch.randn(8, 3, 3, 3, device=dev)
F.conv2d(x, w).sum().item()                # cuDNN
torch.cuda.synchronize()
print(f"torch {torch.__version__} | {torch.cuda.get_device_name(0)}"
      f" | cudnn {torch.backends.cudnn.version()} | matmul+conv2d ok")
""",
    "torch-geometric": """
import torch
assert torch.cuda.is_available(), "torch.cuda.is_available() is False"
import torch_geometric
from torch_geometric.utils import scatter
dev = torch.device("cuda:0")
src = torch.randn(6, 4, device=dev)
index = torch.tensor([0, 1, 0, 1, 2, 1], device=dev)
out = scatter(src, index, dim=0, reduce="sum")
assert out.is_cuda and out.shape == (3, 4)
print(f"torch_geometric {torch_geometric.__version__} | scatter on cuda ok")
""",
    "xgboost": """
import numpy as np
import xgboost as xgb
rng = np.random.default_rng(0)
X = rng.random((256, 8))
y = (X[:, 0] > 0.5).astype(int)
# device="cuda" raises on a CPU-only build or unusable driver — no silent CPU fallback
clf = xgb.XGBClassifier(n_estimators=4, tree_method="hist", device="cuda")
clf.fit(X, y)
clf.predict(X)
print(f"xgboost {xgb.__version__} | hist training on device=cuda ok")
""",
    "numba": """
import numpy as np
from numba import cuda
if not cuda.is_available():
    # most common cause in conda envs: libNVVM not installed
    print("numba.cuda.is_available() is False"
          " — if the driver works for torch, add cuda-nvcc/cuda-nvvm to the env")
    raise SystemExit(1)
@cuda.jit
def add_one(x):
    i = cuda.grid(1)
    if i < x.size:
        x[i] += 1.0
buf = cuda.to_device(np.zeros(64, dtype=np.float32))
add_one[1, 64](buf)
assert (buf.copy_to_host() == 1.0).all(), "kernel ran but result is wrong"
import numba
name = cuda.get_current_device().name
name = name.decode() if isinstance(name, bytes) else name
print(f"numba {numba.__version__} | compiled+ran a kernel on {name}")
""",
    "root": f"""
import ROOT
ROOT.gROOT.SetBatch(True)
ROOT.RooMsgService.instance().setGlobalKillBelow(ROOT.RooFit.ERROR)
# Probe with ROOT's own loader (searches the same paths RooFit would):
# >= 0 means loadable, negative means the optional CUDA backend is absent.
# Silence TUnixSystem's "does not exist" error for the probe itself.
_prev = ROOT.gErrorIgnoreLevel
ROOT.gErrorIgnoreLevel = ROOT.kFatal
_loaded = ROOT.gSystem.Load("libRooBatchCompute_CUDA")
ROOT.gErrorIgnoreLevel = _prev
if _loaded < 0:
    print(f"ROOT {{ROOT.gROOT.GetVersion()}}: no loadable CUDA backend "
          "(libRooBatchCompute_CUDA — expected for conda-forge ROOT builds); "
          "RooFit/TMVA will use CPU only")
    raise SystemExit({SKIP_EXIT})
x = ROOT.RooRealVar("x", "x", -5, 5)
mean = ROOT.RooRealVar("mean", "mean", 0, -1, 1)
sigma = ROOT.RooRealVar("sigma", "sigma", 1, 0.1, 3)
gauss = ROOT.RooGaussian("gauss", "gauss", x, mean, sigma)
data = gauss.generate({{x}}, 2000)
res = gauss.fitTo(data, EvalBackend="cuda", Save=True, PrintLevel=-1)
assert res.status() == 0, f"fit status {{res.status()}}"
print(f"ROOT {{ROOT.gROOT.GetVersion()}} | RooFit EvalBackend=cuda fit ok")
""",
}


def driver_report() -> bool:
    """Print driver/GPU info; False means no point running the checks."""
    smi = shutil.which("nvidia-smi")
    if smi is None:
        print("FATAL: nvidia-smi not on PATH — not a GPU session?")
        return False
    query = subprocess.run(
        [smi, "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if query.returncode != 0:
        # MIG slices sometimes reject --query-gpu; plain nvidia-smi still works
        query = subprocess.run([smi, "-L"], capture_output=True, text=True, timeout=30)
    if query.returncode != 0:
        print(f"FATAL: nvidia-smi failed (driver problem?):\n{query.stderr.strip()}")
        return False
    print(f"driver/GPU: {query.stdout.strip() or '(no output)'}")
    return True


def run_check(name: str, code: str, timeout: float) -> tuple[str, str, float]:
    """→ (status, detail, seconds); status ∈ PASS / FAIL / SKIP / TIMEOUT."""
    start = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return (
            "TIMEOUT",
            f"no result within {timeout:.0f}s (hung driver call?)",
            timeout,
        )
    elapsed = time.monotonic() - start
    stdout_lines = proc.stdout.strip().splitlines()
    output = (proc.stdout + proc.stderr).strip().splitlines()
    if proc.returncode == 0:
        return "PASS", stdout_lines[-1] if stdout_lines else "ok", elapsed
    if proc.returncode == SKIP_EXIT:
        # the skip reason is what WE print to stdout; stderr may hold
        # framework noise (e.g. cling diagnostics)
        return "SKIP", stdout_lines[-1] if stdout_lines else "skipped", elapsed
    # surface the most useful line: last traceback/error line
    detail = output[-1] if output else ""
    if "GLIBCXX" in proc.stdout + proc.stderr:
        detail += (
            "  [hint: system libstdc++ shadows the env's — a pip-installed lib"
            " (no rpath) loaded /usr/lib64/libstdc++ first; retry with"
            " LD_LIBRARY_PATH=$CONDA_PREFIX/lib]"
        )
    return "FAIL", detail, elapsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--only", help="comma-separated subset: " + ",".join(CHECKS), default=""
    )
    parser.add_argument(
        "--timeout", type=float, default=180, help="seconds per framework [%(default)s]"
    )
    args = parser.parse_args()

    selected = [s.strip() for s in args.only.split(",") if s.strip()] or list(CHECKS)
    unknown = set(selected) - set(CHECKS)
    if unknown:
        parser.error(f"unknown check(s): {', '.join(sorted(unknown))}")

    if not driver_report():
        return 1

    print(f"python: {sys.executable}\n")
    failed = False
    for name in selected:
        status, detail, elapsed = run_check(name, CHECKS[name], args.timeout)
        failed |= status in ("FAIL", "TIMEOUT")
        print(f"{status:<7} {name:<16} {elapsed:6.1f}s  {detail}")

    print()
    print(
        "all good — CUDA usable in every checked framework"
        if not failed
        else "FAILURES above — CUDA is not fully usable in this environment"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
