# Slurm probes

Probes that submit nothing but ask each interLink cluster's Slurm controller a
question. Today there is one: how long a job would wait before starting, per
cluster and per account/partition/QoS selector.

```
af_slurm_backlog_seconds{cluster="gautschi",account="cms",partition="cpu",qos="standby",flags="--account=cms --partition=cpu --qos=standby"} 291360
```

`sbatch --test-only` validates the account/partition/QoS combination and
reports when a job WOULD start, without submitting. That catches what capacity
metrics miss: the reading above is 3.4 days, taken while the same partition
showed 10,246 idle CPUs — `standby` yields to owners.

`account`, `partition` and `qos` are the whole eligibility selector. Slurm has
no "queue" separate from the partition, and job shape does not move the
prediction — measured identical for 1 core / 5 min and 64 cores / 4 h on both
Hammer and Gautschi. The labels are parsed from the flags actually submitted,
so they cannot describe a different job than the one measured. `flags` is
the same string unsplit — the Grafana legend prints it verbatim so a user can
line it up against their own `sbatch` invocation.

QoS is the axis that dominates: on the same account and partition, Gautschi
predicted 3.4 days with `--qos=standby` and 1.9 days without it.

A failed probe emits **no series at all**, rather than zero: an unknown wait is
not a short one. The reason goes to the container log.

## Shape

One probe container per cluster, because each needs its own munge key and
Slurm client config. Each runs the plugin image and chains `/etc/startup.sh`,
which installs that cluster's config and starts `munged`, then loops
`probe.sh` every 5 minutes. Probes write a textfile into a shared `emptyDir`;
a small Python sidecar serves them on `:9100`, since the plugin image has no
Python.

The probe submits with `--uid=616617`. The container runs as root, and root's
Slurm associations are not a user's — testing as root reports a false
`Invalid account or account/partition combination` on Gautschi.

Flags come from the same table the interLink Dask gateway uses
(`INTERLINK_CLUSTERS` in `apps/dask-gateway/dask-gateway-k8s-interlink`); a
test asserts they match, since a wait measured for a job nobody submits is
worthless.

## When a cluster reports nothing

```bash
kubectl -n cms logs deploy/slurm-probes -c probe-<cluster>
```

`Protocol authentication error` with the controller port reachable and local
`munged` healthy is either a bad munge key **or** a Slurm client/controller
version mismatch (Negishi is 24.11; Hammer/Gautschi are 25.11 — see
`slurm/client-versions`).
