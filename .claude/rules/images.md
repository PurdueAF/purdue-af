---
paths:
  - "docker/**"
  - "pixi/**"
---

# Images and environments

- A change to a build context mints a new image. What that image reaches, and
  when: [RELEASING.md](../../RELEASING.md).
- `pixi/base` (inside the session image) and `pixi/global` (synced to
  `/work/pixi/global`) are the analysis environments the platform ships. A
  component's own dependency does not go there; the component gets its own
  `pixi.toml`. For these two, edit the `pixi.toml` only: CI regenerates the
  lock and commits it to the branch.
- Images are `linux/amd64`; on an arm64 machine build with
  `--platform linux/amd64`.
