# dask-gateway

The [Dask Gateway](https://gateway.dask.org) chart, `dask-gateway` from
`helm.dask.org`, that gives each AF user and each
[workload account](#workload-accounts) a Dask cluster on the AF nodes, running
as them. How users create and size one:
[Dask Gateway at Purdue AF](../../docs/docs/guide-dask-gateway.md).

| File               | What it is                                                                                                                                                                                                                                                                                           |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `helmrepo.yaml`    | `HelmRepository` for the Dask charts                                                                                                                                                                                                                                                                 |
| `helmrelease.yaml` | The release: its values from `values.yaml`, the API pod's storage and `af-accounts` mounts, and the controller's launch command, which makes workers past a cluster's floor preemptible, starts those only while the namespace quotas keep a reserve free, and serves the `/healthz` its probes read |
| `values.yaml`      | The chart's values: the schedulers' and workers' pods, and in `gateway.extraConfig` who a request is, the options a cluster takes, whom it runs as, and the cap on a user's active clusters                                                                                                          |
| `rbac.yaml`        | The controller's read of the namespace's ResourceQuotas                                                                                                                                                                                                                                              |
| `ingress.yaml`     | `dask-gateway.geddes.rcac.purdue.edu`, through the chart's Traefik                                                                                                                                                                                                                                   |
| `aliases.yaml`     | The same gateway at `dask-gateway-k8s.geddes.rcac.purdue.edu`, for sessions whose `DASK_GATEWAY__*` variables name it                                                                                                                                                                                |

## Workload accounts

A workload account is a workload's own user of this gateway and of the
[Ray gateway](../ray-train/README.md). The workload declares it in its app
directory, in `account.yaml`:

```yaml
name: <name>
uid: <uid>
gid: <gid>
tokenSha256: <SHA-256 of the token, lowercase hex>
```

Beside it are the PVC `<name>` and `secret-account.yaml`, the SOPS-encrypted
Secret `<name>-account`, whose key `token` holds the account's token: random
hex. The Flux root that deploys both gateways holds each declaration in its
`af-accounts` ConfigMap as `<name>.yaml`, and lists the PVC and the Secret.
Both gateways mount `af-accounts` at `/etc/af-accounts` and read it on every
request; without it, there are no accounts.

A client sends the token as `Authorization: Bearer <token>`. This gateway
checks it on every request, setting no session cookie for it, and takes it
only from a pod's own address in the pod network, `POD_NETWORK` in
`values.yaml`, on a request with no `X-Forwarded-For`, `Forwarded` or
`X-Real-Ip` header: from a pod, at `api-dask-gateway:8000`. The Ray gateway
takes it as Ray Client and the Jobs API's client send `RAY_AUTH_TOKEN`. Each
gateway looks up the token's SHA-256 among the declarations, and logs each
refusal as `refused: reason=<reason>`, with the account's name when it knows
it.

An account has a Dask cluster and a Ray cluster as a user has, under the same
caps. Their pods run as its `uid` and `gid`, mount its PVC at `/<name>`,
read-write, and CVMFS, and no other storage, and carry the label
`app.kubernetes.io/part-of: <name>`. A cluster's environment is under
`/<name>` or `/cvmfs`; this gateway checks one on CVMFS for
`prometheus_client`, and not one under `/<name>`, which its API pod does not
mount. The account's Dask workers run without the worker preload, which is on
`/work`.

The monitoring and the logs see the account as the user `<name>`: its Dask
pods carry the label `user: <name>` and have `USER`, `LOGNAME`, `NB_USER` and
`JUPYTERHUB_USER` set to `<name>`, and its Ray cluster is `ray-train-<name>`,
the name a user `<name>`'s would have, with `USER` set to `<name>`. In this
gateway its Dask user is `account:<name>`, a name no user has. Both gateways
refuse a user whose name is a declared account's or holds a `:`, this gateway
a session cookie of such a user too, and the Ray gateway a Hub token of
anything but a user. The Ray gateway relays a call only to a cluster that runs
as its caller, by the cluster's UID, GID and `app.kubernetes.io/part-of`
label.

The gateways refuse, and log, a declaration with other keys; a name that is no
lowercase DNS label of at most `ACCOUNT_NAME_MAX` characters, or that is in
`RESERVED_NAMES`, starts with `claim-` or ends with `-cern` or `-fnal`; a `uid`
or `gid` outside `ACCOUNT_IDS`; a `tokenSha256` other than lowercase hex of a
SHA-256; and every declaration that shares its name or its hash with another.

To add one, in the workload's app directory and its Flux root:

1. a token, written only encrypted, and its `tokenSha256` for `account.yaml`,
   from the repository's root:

   ```bash
   token=$(openssl rand -hex 32)
   printf %s "$token" | shasum -a 256
   printf 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: <name>-account\ntype: Opaque\nstringData:\n  token: %s\n' "$token" | sops --encrypt --filename-override apps/<app>/secret-account.yaml --input-type yaml --output-type yaml --indent 2 /dev/stdin > apps/<app>/secret-account.yaml
   ```

2. `volume.yaml`, the PVC `<name>`;
3. `<name>.yaml=../../apps/<app>/account.yaml` in the root's `af-accounts`
   generator, and the Secret and the PVC in its `resources`.

```bash
kubectl -n cms get configmap af-accounts -o yaml
kubectl -n cms logs deploy/api-dask-gateway | grep 'refused: reason='
kubectl -n cms logs deploy/ray-train-gateway | grep 'refused: reason='
```
