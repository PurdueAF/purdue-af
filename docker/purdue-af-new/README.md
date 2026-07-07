# purdue-af-new — experimental slim rebuild (0.13.0-preX)

Rebuild of [`docker/purdue-af`](../purdue-af/Dockerfile) on top of NVIDIA's
official CUDA base image instead of CERN alma8-base + a hand-installed
`cuda-toolkit-12-4`. Motivated by a layer-by-layer analysis of the
0.12.5 build (30m30s, 7.6 GB compressed) with
[`analyze_image_build.py`](../kaniko-build-jobs/analyze_image_build.py):

| finding in 0.12.5                                                                                      | fix here                                                                                                                                                                                         |
| ------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 17m28s dnf step; `cuda-toolkit-12-4` meta-package pulls nsight-compute **and** nsight-systems (2.4 GB) | `FROM nvidia/cuda:12.4.1-devel-rockylinux8` — CUDA comes as immutable, NVIDIA-maintained, LAN-cached base layers (3.7 GB compressed; still includes nsight-compute + NCCL, drops nsight-systems) |
| ~1.2 GB kernel firmware from alma8-base + `dnf upgrade`                                                | Rocky base has none; `--exclude='linux-firmware*'` guards the upgrade                                                                                                                            |
| 142 MB Java 8 JDK dragged in by `voms-clients` (Java implementation)                                   | `voms-clients-cpp`                                                                                                                                                                               |
| alma8-base is a 678 MB single layer                                                                    | Rocky OS layer is 69 MB                                                                                                                                                                          |
| CERN krb5/CA defaults came from alma8-base                                                             | vendored in `configs/` (extracted byte-identical from `alma8-base:20250501-1`): `krb5.conf.d/cern-*` for `kinit <user>@CERN.CH` (eos-connect.sh), `CERN-bundle.pem`                              |
| alma8-base preinstalled CLI basics the Rocky+CUDA base lacks ("ps: command not found" in pre1)         | `procps-ng psmisc krb5-workstation xz cpio` added (from an `rpm -qa` diff of the two bases; krb5-workstation is the critical one — kinit/klist for EOS)                                          |

Everything else — OSG rpms, Slurm, user setup, the pixi stack, the runtime
stage — matches the original stage for stage, and reuses the files in
`docker/purdue-af/` (this directory only adds the Dockerfile and the CERN
configs).

One gotcha, learned from the first pre1 build attempt: the base image's
`cuda.repo` is NVIDIA's **rolling** rhel8 repo, which also contains CUDA 13.x
whose `cccl` package _obsoletes_ `cuda-cccl-12-4` — so a plain `dnf upgrade`
tries to cross-grade CUDA and fails dependency resolution. All dnf commands
therefore run with `--disablerepo=cuda` except the exact-version cuDNN
install; CUDA stays at the base image's pins. (Resolution of every dnf
transaction was dry-run-verified in the amd64 base image before the second
build attempt.)

Expected: **~5 GB compressed** (from 7.6) and roughly half the build time.

## Build

The Job builds from the `main` branch, so changes must be merged first:

```
kubectl apply -n cms -f docker/kaniko-build-jobs/build-af-new.yaml
```

Pushes to `geddes-registry.rcac.purdue.edu/cms/purdue-af:0.13.0-pre2`
(pre1 built in ~10 min but was missing procps/krb5-workstation — see the
base-parity row above; bump the tag in the job YAML per iteration).

## CI/CD pipeline (pre-release channel)

`.github/workflows/e2e-hub.yml` owns this image end to end:

1. **build-image** (when a commit touches this dir, `pixi/base/`, or the
   Slurm inputs): buildx build with the geddes `FROM` remapped to docker.io
   via a named context, smoke test (nvcc, ps, klist, xz, jupyterlab), CVMFS
   test (host CVMFS mounted in, `cmsset_default.sh` sourced), then push of
   the immutable `ghcr.io/purdueaf/purdue-af:sha-<commit>`.
2. **e2e-prerelease**: full hub-in-kind e2e that spawns THIS exact image
   through the hub's `pre-release` profile and asserts the pod runs it and
   JupyterLab answers. When the commit didn't rebuild the image, it
   revalidates the currently promoted `:pre-release` tag instead (also
   weekly via cron).
3. **promote** (main only, after e2e passes): moves the
   `ghcr.io/purdueaf/purdue-af:pre-release` tag to the tested digest.

The hub's "Latest pre-release version" profile
(`apps/jupyterhub/jupyterhub/values.yaml`) pulls `:pre-release` with
`image_pull_policy: Always`, so validated builds reach user sessions
automatically — no manifest edit, no hand-pushed tags. The production
profile stays pinned; moving it to the same flow is the follow-up step.

⚠ One-time manual step: make the `purdue-af` package public on
ghcr.io/purdueaf after the first push (cluster nodes pull it
unauthenticated), or configure an imagePullSecret / the ghcr-cache Harbor
proxy from docker/REGISTRY.md instead.

The kaniko Job below remains as a cluster-local fallback build path.

## Compare against 0.12.5

```
kubectl logs -n cms job/kaniko-build-af-new | \
    python3 docker/kaniko-build-jobs/analyze_image_build.py --log - \
        --image geddes-registry.rcac.purdue.edu/cms/purdue-af:0.13.0-pre2
```

## Things to verify on a test session before promoting

- [ ] GPU session: `nvidia-smi`, `nvcc --version`, torch/TF see the GPU
      (system cuDNN is 8.9.7, same pin as 0.12.x)
- [ ] **Non-GPU session does NOT see GPUs** — the NVIDIA base image sets
      `NVIDIA_VISIBLE_DEVICES=all`; confirm the cluster's container runtime
      only honours it for pods that request a GPU resource
- [ ] `eos-connect.sh`: `kinit <user>@CERN.CH` resolves the realm and EOS
      mounts (krb5 snippets vendored from alma8-base)
- [ ] Grid workflows: `voms-proxy-init` (now the C++ client), gfal2, xrootd
      via CVMFS site config
- [ ] Slurm: `sbatch`/`squeue` against hammer
- [ ] Anything that assumed Alma-specific packaging (should be none —
      Rocky 8 and Alma 8 are both EL8)

Promotion path: once validated, this Dockerfile replaces
`docker/purdue-af/Dockerfile` (keeping that directory's scripts/configs,
which are referenced unchanged), this directory is deleted, and the regular
`build-af.yaml` job bumps to 0.13.0.
