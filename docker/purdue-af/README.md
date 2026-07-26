# purdue-af — the Analysis Facility singleuser image

The image every AF session runs. Built on
`nvidia/cuda:12.4.1-devel-rockylinux8`, ~5 GB compressed. This directory is
self-contained: Dockerfile, jupyter configs, startup scripts, OSG rpms,
code-server assets, the pixi wrapper, and the CERN krb5/CA defaults under
`configs/`.

Contents beyond the CUDA base: the pixi `base-env` (`pixi/base/` — JupyterLab,
code-server, kernels), OSG grid clients (`voms-clients-cpp`, gfal2, xrootd),
Slurm client with Hammer configs, and CVMFS/XRootD site config.

Notable constraints baked into the Dockerfile:

- Every `dnf` command runs with `--disablerepo=cuda` except the pinned cuDNN
  install. The base image ships NVIDIA's **rolling** rhel8 repo, which also
  carries CUDA 13.x whose `cccl` obsoletes `cuda-cccl-12-4`; an unconstrained
  `dnf upgrade` cross-grades CUDA and fails resolution.
- `--exclude='linux-firmware*'` on the upgrade — kernel firmware is useless
  in a container and costs >1 GB.
- `procps-ng psmisc krb5-workstation xz cpio libatomic` are installed
  explicitly: the CUDA base lacks them and sessions need them
  (`krb5-workstation` provides the `kinit`/`klist` that `eos-connect.sh` uses).
- `LIBRARY_PATH` points at CUDA stub libs (inherited from the base) so `nvcc`
  can link without a driver present.
- The image does **not** set `NVIDIA_VISIBLE_DEVICES`. The base bakes `all`,
  and the cluster runtime honours it, so 0-GPU sessions are guarded at spawn
  time instead: `gpu-availability.py`'s `modify_pod_hook` injects `void` into
  pods that request no GPU. GPU sessions get visibility from the device
  plugin's per-allocation injection.

## Build and publication

`ci.yml` owns this image end to end; there is no other build path.

1. **build-af-image** (`ci-images.yml`) — the image is content-addressed:
   tagged `in-<hash>` of its input tree (this directory, `pixi/base/`, the
   Slurm inputs; see `.github/workflows/image-inputs.sh`). An existing tag is
   verified reuse. Otherwise buildx builds it with the geddes `FROM` remapped
   to docker.io via a named context, the smoke stage runs during the build
   (nvcc, ps, klist, xz, jupyterlab), and a single zstd upload pushes image
   and `mode=max` buildcache as deduplicated blobs.
2. **e2e-pre-release** (`ci-e2e.yml`) — pulls the `in-` image once, runs the
   CVMFS check (host CVMFS mounted in, `cmsset_default.sh` sourced), `kind
load`s the same copy, then the hub-in-kind e2e spawns it through the hub's
   `pre-release` profile and asserts JupyterLab answers.
3. **publish** (`ci.yml`, main only, behind `ci-ok`) — adds `:sha-<commit>`
   and moves `:pre-release` to the tested digest.

The hub's pre-release profile pulls `:pre-release` with `image_pull_policy:
Always`, so validated builds reach sessions on the next spawn. Production
stays pinned to a semver tag.

Images are public on ghcr; manifests pull them through the geddes
`ghcr-proxy-cache` Harbor project, which revalidates moving tags upstream on
each pull. See [REGISTRY.md](../REGISTRY.md).

## Releasing a new version

**Release image** (`workflow_dispatch`) promotes the soaking `:pre-release`
digest to a semver tag. Two gates: the release commit's `ci-ok` must be
green, and the digest must be both the `in-<hash>` image of the current repo
state and the current `:pre-release` — the exact bytes that were tested. It
then adds the semver tag to that same digest (never a rebuild), rewrites
every version spot in `values.yaml` (`bump-af-version.py`, count-verified),
commits, tags `v<version>`, and publishes a GitHub Release.

The bump commit reaches production with the next platform tag. Bump rules and
rollback: [RELEASING.md](../../RELEASING.md).

Before releasing, verify on a test session: GPU visibility (`nvidia-smi`,
torch/TF), that a 0-GPU session sees no GPUs, `eos-connect.sh` (kinit against
CERN.CH), grid workflows (`voms-proxy-init`, gfal2, xrootd), and Slurm
(`sbatch`/`squeue`).
