---
paths:
  - "apps/monitoring/grafana/**"
---

# Grafana

- Time series are unstacked lines with no fill and no bars: a stacked top band
  reads as the whole series.
- A sparse metric (a few events an hour) is queried at a fixed `1h` step, not
  `$__rate_interval`, or the panel is empty under a 7-day range.
- Provisioning cannot change a datasource's `uid` over existing state; the
  instance crash-loops. Remove it with `deleteDatasources` and re-add it in
  the same file.
