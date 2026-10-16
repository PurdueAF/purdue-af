# Alloy

Grafana Alloy, a DaemonSet in `cms` that ships logs to Loki. Each pod tails
the containers of the `cms` pods on its own node. The pods also form an Alloy
[cluster](https://grafana.com/docs/alloy/latest/get-started/clustering/), and
one of them watches the Kubernetes events in `cms`
([`loki.source.kubernetes_events`](https://grafana.com/docs/alloy/latest/reference/components/loki/loki.source.kubernetes_events/)),
so Loki keeps each event after Kubernetes expires it.

| path               | what it is                                                               |
| ------------------ | ------------------------------------------------------------------------ |
| `helmrelease.yaml` | the `alloy` chart release                                                |
| `values.yaml`      | chart values; `alloy.configMap.content` is the whole Alloy configuration |

## Querying events

Event streams carry `job="loki.source.kubernetes_events"` and the involved
object's `kind`, the event's `reason` and `type` as labels; the object's name
is the structured metadata `name`. A session pod's mount failures, with the
node that reported them, in the private Grafana's `loki` data source:

```logql
{job="loki.source.kubernetes_events", kind="Pod", reason="FailedMount"} | name=~"purdue-af-.+" | logfmt | line_format "{{.sourcehost}} {{.name}} {{.msg}}"
```

The rest of the query language: [LogQL](https://grafana.com/docs/loki/latest/query/).
