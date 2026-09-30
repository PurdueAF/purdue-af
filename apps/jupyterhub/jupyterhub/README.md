# jupyterhub

The Hub: the [Zero to JupyterHub](https://z2jh.jupyter.org) chart, its values,
and the config snippets the Hub loads from `jupyterhub_config.d`. Which
cluster runs it, and how the snippets become ConfigMaps, is the Flux roots'
([deploy/README.md](../../../deploy/README.md)).

| File                                | What it is                                                                        |
| ----------------------------------- | --------------------------------------------------------------------------------- |
| `helmrelease.yaml`, `helmrepo.yaml` | The chart release                                                                 |
| `values.yaml`                       | Chart values: profiles, storage, services, roles                                  |
| `secret-auth.yaml`                  | The CILogon client credentials, SOPS-encrypted                                    |
| `rbac.yaml`                         | Lets the Hub keep the pooled-account ledger                                       |
| `extraFiles/custom-spawner.py`      | The CILogon authenticator: usernames, allow-lists, stale profile options          |
| `extraFiles/set-user-info.py`       | The UID/GID a session runs as: LDAP for Purdue users, a pooled account for others |
| `extraFiles/gpu-availability.py`    | The GPU choices of the spawn form and the gate behind them                        |
| `extraFiles/session-activity.py`    | Session activity on `/hub/metrics`                                                |
| `extraFiles/ray-train.py`           | The Ray Client environment of a session                                           |
| `extraFiles/cull-gpu-sessions.py`   | The `gpu-culler` service                                                          |
| `extraFiles/gpu_queries.py`         | PromQL shared with the agentic interface                                          |

## Pooled accounts

A user from outside Purdue has no LDAP account of their own and runs as one
of the pooled `paf` accounts. Which one is recorded in the ConfigMap
`af-pooled-accounts` in `cms`: one key per username, the account as the
value. The Hub owns it. `set-user-info.py` creates it from the Hub's users
when it is missing, picks the account of a user without an entry
(`free_account` there), and never changes an entry that exists. It is not in
git, and Flux does not manage it. The Ray Train gateway reads it to run a
user's cluster as the same account ([apps/ray-train](../../ray-train/README.md)).

An entry whose key is not a username holds its account all the same.
`deleted-<hub id>` reserves the account of a hub id that belongs to no user:
files it owns may still exist under `/work`. Free one by deleting the entry,
once nothing under `/work` is owned by that account.

```bash
kubectl -n cms get configmap af-pooled-accounts -o yaml
```
