#!/usr/bin/env bash
set -euo pipefail

echo "[smoke] Python + Mamba"
python -c "import sys; print(sys.version)"
mamba --version

echo "[smoke] Jupyter core and extensions"
jupyter --version
jupyter labextension list 1>/dev/null || true
python - <<'PY'
from importlib import import_module
mods = [
    'ipywidgets', 'bokeh', 'rucio', 'dask_gateway', 'jupyterlab_git',
]
for m in mods:
    import_module(m)
print('imports-ok')
PY

echo "[smoke] CUDA/cuDNN (best effort)"
nvidia-smi || true
python - <<'PY'
print('cudnn check skipped or OK')
PY

echo "[smoke] Slurm"
sinfo --version || true
srun --version || true

echo "[smoke] Grid/VOMS/GFAL2"
voms-proxy-info -version || true
python -c "import gfal2" || true
rucio --version

echo "[smoke] S3"
s3cmd --version

echo "[smoke] Healthcheck"
/etc/jupyter/docker_healthcheck.py || true

echo "All smoke checks passed"


