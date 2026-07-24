# purdue-af — the Analysis Facility singleuser image

The canonical AF image (0.13.0+), built and published by CI (see the
pipeline below). Rebuilt on top of NVIDIA's official CUDA base image
instead of CERN alma8-base + a hand-installed `cuda-toolkit-12-4` — the
prior 0.12.x image lives, retired, in
[`docker/purdue-af-old`](../purdue-af-old/Dockerfile). Motivated by a
layer-by-layer analysis of the 0.12.5 build (30m30s, 7.6 GB compressed):

| finding in 0.12.5                                                                                      | fix here                                                                                                                                                                                         |
| ------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 17m28s dnf step; `cuda-toolkit-12-4` meta-package pulls nsight-compute **and** nsight-systems (2.4 GB) | `FROM nvidia/cuda:12.4.1-devel-rockylinux8` — CUDA comes as immutable, NVIDIA-maintained, LAN-cached base layers (3.7 GB compressed; still includes nsight-compute + NCCL, drops nsight-systems) |
| ~1.2 GB kernel firmware from alma8-base + `dnf upgrade`                                                | Rocky base has none; `--exclude='linux-firmware*'` guards the upgrade                                                                                                                            |
| 142 MB Java 8 JDK dragged in by `voms-clients` (Java implementation)                                   | `voms-clients-cpp`                                                                                                                                                                               |
| alma8-base is a 678 MB single layer                                                                    | Rocky OS layer is 69 MB                                                                                                                                                                          |
| CERN krb5/CA defaults came from alma8-base                                                             | vendored in `configs/` (extracted byte-identical from `alma8-base:20250501-1`): `krb5.conf.d/cern-*` for `kinit <user>@CERN.CH` (eos-connect.sh), `CERN-bundle.pem`                              |
| alma8-base preinstalled CLI basics the Rocky+CUDA base lacks ("ps: command not found" in pre1)         | `procps-ng psmisc krb5-workstation xz cpio` added (from an `rpm -qa` diff of the two bases; krb5-workstation is the critical one — kinit/klist for EOS)                                          |

Everything else — OSG rpms, Slurm, user setup, the pixi stack, the runtime
stage — matches the original stage for stage. This directory is
self-contained: the shared scripts, jupyter configs, OSG rpms, xml,
code-server assets, and pixi-wrapper were consolidated here from the
legacy image (the CERN krb5/CA configs are under `configs/`).

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

CI builds and publishes this image — see the pipeline below. There is no
other live build path.

## CI/CD pipeline (pre-release channel)

The staged `ci.yml` pipeline owns this image end to end:

1. **build-af-image** (`ci-images.yml`): the image is CONTENT-ADDRESSED —
   tagged `in-<hash>` of its input tree (this self-contained dir,
   `pixi/base/`, the Slurm inputs; see
   `.github/workflows/image-inputs.sh`). If the tag already exists on ghcr
   the build is verified reuse; otherwise: buildx build with the geddes
   `FROM` remapped to docker.io via a named context, the basic smoke test
   (nvcc, ps, klist, xz, jupyterlab) running DURING the build as the
   Dockerfile's `smoke` stage, then a single zstd upload from buildx —
   image and `mode=max` buildcache share blobs, so the registry
   deduplicates them, and the image is never loaded into the runner's
   docker daemon.
2. **e2e-pre-release** (`ci-e2e.yml`): pulls the `in-` image ONCE on the
   runner, runs the CVMFS check there (host CVMFS mounted in,
   `cmsset_default.sh` sourced), `kind load`s the same copy, then the full
   hub-in-kind e2e spawns it through the hub's `pre-release` profile and
   asserts the pod runs it and JupyterLab answers.
3. **publish** (`ci.yml`, main only, behind the ci-ok gate — every stage
   of the same commit green): adds `:sha-<commit>` and moves
   `ghcr.io/purdueaf/purdue-af:pre-release` to the tested digest.

The hub's "Latest pre-release version" profile
(`apps/jupyterhub/jupyterhub/values.yaml`) pulls `:pre-release` with
`image_pull_policy: Always`, so validated builds reach user sessions
automatically — no manifest edit, no hand-pushed tags. The production
profile stays pinned; moving it to the same flow is the follow-up step.

Pull path: the ghcr `purdue-af` package is public (2026-07-16; anonymous
pull verified, 5.2 GiB compressed), and manifests pull via the geddes
`ghcr-proxy-cache` Harbor project — LAN-local layers, with Harbor
revalidating moving tags (`:pre-release`, `:latest`) upstream on each
pull, so promotions still land on the next session spawn.

## Old-vs-new comparison (2026-07-16, 0.12.5 vs 0.13.0-pre1 + parity fixes)

Full rpm/command/config diff via throwaway pods on the cluster. Confirmed
identical: entrypoint/cmd/user, all jupyter scripts+configs, Slurm 25.11.4,
krb5 config, pixi base env, voms-proxy-\*/gfal-\*/xrdcp/apptainer.
**Deliberate removals to announce with the release:**

- `nsys`/Nsight Systems, `nvvp` GUIs (the new image instead puts `nvcc`,
  `ncu`, `nvprof`, `cuda-gdb` on PATH — the old image had none of them
  reachable); NCCL 2.21.5 is a new addition
- `java` (openjdk 8, was only there for voms-clients-java; the C++ voms
  client replaces it — conda `openjdk` via pixi if anyone needs java)
- GUI leftovers of the nsight stack (mesa-EGL, nss/nspr, xcb/xorg fonts) —
  basic X11+GLX kept explicitly
- known quirk inherited from the NVIDIA base: `LIBRARY_PATH` points at CUDA
  _stub_ libs (intended: lets `nvcc` link without a driver)

**GPU exposure (fixed at spawn time, not in-image):** the NVIDIA base
bakes `NVIDIA_VISIBLE_DEVICES=all` and the cluster runtime honours it — a
0-GPU pod on paf-a01 saw both T4s. The fix lives in the hub, not the
image: `gpu-availability.py`'s `modify_pod_hook` injects
`NVIDIA_VISIBLE_DEVICES=void` into the pod spec of sessions that request
no GPU (unit-tested). The image deliberately does NOT override the env:
GPU sessions keep exactly the 0.12.x semantics — visibility comes from
the device plugin's per-allocation injection, with nothing new for it to
fight — so a GPU-session regression from this change is structurally
impossible. (An earlier iteration baked `void` into the image; reverted
in favor of the spawn-time guard.)

## Things to verify on a test session before promoting

- [ ] GPU session: `nvidia-smi`, `nvcc --version`, torch/TF see the GPU
      (system cuDNN is 8.9.7, same pin as 0.12.x)
- [ ] **Non-GPU session does NOT see GPUs** — verified BROKEN with the raw
      image on the cluster (0-GPU pod saw all node GPUs); now guarded by
      the spawn-time `void` injection — verify on a 0-GPU session after
      the hub config rolls
- [x] `eos-connect.sh`: `kinit <user>@CERN.CH` resolves the realm and EOS
      mounts (krb5 snippets vendored from alma8-base) — verified 2026-07-20
- [x] Grid workflows: `voms-proxy-init` works (C++ client); gfal2/xrootd
      configs byte-identical
- [x] Slurm: `sbatch`/`squeue` against hammer — verified 2026-07-20
- [x] Alma-specific packaging: rpm diff shows nothing user-relevant beyond
      the items listed above (docs updated to say "EL8" instead of
      "AlmaLinux8")

## Promotion to production (release workflows)

Release channels, each with exactly ONE minting trigger (so a wrong-stream
bump is structurally impossible):

| channel                                                             | scheme                                                         | minted by                                                             |
| ------------------------------------------------------------------- | -------------------------------------------------------------- | --------------------------------------------------------------------- |
| platform                                                             | `YYYY.M.SEQ` CalVer (repo tag + GitHub Release, no file edits) | **Release platform** (release-platform.yml) dispatch                  |
| purdue-af image                                                      | its own semver `0.X.Y` (repo tag `v0.X.Y`)                     | **Release image** (release-image.yml) dispatch, `patch\|minor\|major` |
| all aux images (agentic-interface, af-pod-monitor, af-node-monitor) + pre-release | `:latest` / `:pre-release` / `:sha-` / `:in-` moving tags | ci.yml only, never humans                                             |

**Release image** promotes the purdue-af image behind two gates: the
release commit's `ci-ok` check must be green (the checks API is queried —
commits whose CI never ran fail loudly), and the digest being promoted
must be the `in-<hash>` image of the CURRENT repo state AND equal the
soaking `:pre-release` digest — the exact bytes users tested. It then
adds the immutable semver tag to the SAME digest (promote-by-digest —
never a rebuild), rewrites every version spot in values.yaml
(`bump-af-version.py`, count-verified), commits, tags `v<version>`, and
publishes a GitHub Release with digest provenance.

**Continuous channel**: the ci.yml publish stage moves the ghcr `:latest`
tag after every fully green main pipeline (lint + unit + manifests +
builds + pixi + e2e); the agentic-interface, af-pod-monitor and
af-node-monitor manifests pull it via the geddes ghcr-proxy-cache
(kubernetes pulls `:latest` with policy Always by default), so those
deploy continuously from any state that passed everything.

Flux rolls the hub config; sessions pull the pinned semver via the geddes
ghcr-proxy-cache. Rollback = `git revert` the release commit (old tags
stay on ghcr). ⚠ One-time setup: add a fine-grained PAT with
`contents: write` as the `AF_RELEASE_TOKEN` secret — pushes made with the
default GITHUB_TOKEN don't trigger CI on the release commit.

**Cutover status:** complete. The first release (0.13.0, 2026-07) landed,
production runs this CI-built image via the geddes ghcr-proxy-cache, and
`e2e-production` spawns the pinned tag every run. This directory is now the
canonical `docker/purdue-af/` and is self-contained (the shared
scripts/configs were consolidated in from the legacy image). The retired
0.12.x image is `docker/purdue-af-old/Dockerfile`;
it is kept for reference only and is no longer a live build path.
