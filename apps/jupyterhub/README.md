# JupyterHub

Flux deploys the `jupyterhub` HelmRelease from `jupyterhub/` with the values
in `jupyterhub/values.yaml` and the config snippets in `jupyterhub/extraFiles/`
(see `deploy/*/kustomization.yaml` for the wiring).

## LDAP uid/gid lookups on geddes-auth

Purdue accounts get their `NB_UID`/`NB_GID` from an anonymous LDAP search on
`geddes-auth.rcac.purdue.edu` at spawn time (`extraFiles/set-user-info.py`,
base `ou=AllPeople,dc=geddes,dc=rcac,dc=purdue,dc=edu`). The same lookup runs
in both Dask Gateway `options_handler`s and in the Slurm gateway backend, so a
Dask cluster gets the same uid/gid as the notebook.

### The server only answers from the 172.21.160.0/24 node subnet

geddes-auth applies a source-address ACL. From a pod on a `geddes-b*`/`geddes-g*`
node (172.21.160.0/24) the anonymous search returns the entry; from a pod on a
`paf-*` node (172.18.29.0/24), or from the cluster's other node subnets, the
anonymous bind succeeds but every search, even a base read of the server's
naming context, comes back `32 noSuchObject`. Pod traffic leaves the cluster
with the node's address, so what matters is the node the pod landed on.

Until RCAC adds the other node subnets to that ACL (at least 172.18.29.0/24;
the cms-af-prod pool also spans 172.18.0.0/24, 172.18.36.0/24 and
172.18.49.0/24), the hub and the three `api-dask-gateway-*` pods are pinned to
the allowed nodes with `role: storage-node` in their `nodeSelector`. That label
is carried by exactly the geddes-b*/g* cms-af-prod nodes. Remove it once the
ACL is widened.

### It is slow

From the cluster a subtree search takes 15-35 s (a base read is instant, so
this is the backend, not the network). The lookup therefore bounds its receive
timeout at 120 s, and the hub hook awaits it in a worker thread so the hub's
event loop keeps serving other requests during a spawn. The gateway
`options_handler`s are still synchronous.

### What a failure looks like

The lookup checks the bind and the search result code and raises with the LDAP
result (`32 noSuchObject`, `49 invalidCredentials`, ...) instead of the old
`IndexError`. A genuinely unknown account raises `no LDAP entry for uid=...`.
Check `kubectl logs -n cms -l component=hub` for the `{'entries': ...}` line
and the exception right after it.

Handy read-only probe from inside a pod (the hub image has `ldap3`):

```bash
kubectl exec -n cms deploy/hub -- python3 -c 'from ldap3 import *; s=Server("geddes-auth.rcac.purdue.edu",port=636,use_ssl=True); c=Connection(s,authentication=ANONYMOUS,receive_timeout=120); c.bind(); c.search("ou=AllPeople,dc=geddes,dc=rcac,dc=purdue,dc=edu","(uid=so44*)",SUBTREE,attributes=["uidNumber","gidNumber"]); print(c.result, c.entries)'
```
