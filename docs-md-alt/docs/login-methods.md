# Login methods and usernames

User authentication at Purdue AF is implemented using **CILogon**.
Three login methods are available:

* Purdue University account (BoilerKey)
* CERN account (CMS users only)
* FNAL account

For Purdue and CERN users, the usernames are compared against a list of allowed
users in order to prevent abuse of resources:

* for **Purdue**, the allowed users are all users with access to the Hammer cluster;
* for **CERN**, the allowed users are required to be members of the CMS VO.

## How usernames are constructed

At login, the username and the hostname are constructed as follows:

| Login method   | Example username | Example hostname |
| -------------- | ---------------- | ---------------- |
| Purdue account | `dkondra`        | `purdue-af-1`    |
| CERN account   | `dkondrat-cern`  | `purdue-af-2`    |
| FNAL account   | `dkondrat-fnal`  | `purdue-af-3`    |

The AF username starts with the username taken from your login credentials, which
may or may not be the same for different accounts — and may even coincide between
different people at different institutions. To avoid naming conflicts, CERN and
FNAL usernames are amended with `-cern` and `-fnal` suffixes, respectively, while
Purdue usernames are left unchanged. Since usernames are unique within each
institution, this guarantees that a new user cannot accidentally get access to
another user's data.

!!! note "One person, multiple accounts"

    The same person using different login credentials is treated as **different
    users**: each login method gets its own home directory, work area, and storage
    quotas. Pick one login method and stick with it, otherwise your files will be
    scattered across several accounts.

!!! tip "Where the suffix matters"

    Remember the `-cern` / `-fnal` suffix whenever you are asked for your Purdue AF
    username outside of the browser — for example when
    [connecting via SSH](guide-ssh-access.md) or
    [from a local IDE](guide-ide-connection.md).

## Account permissions at a glance

Some features are only available to users with a local Purdue account, due to
Purdue data access policies:

| Capability                                  | Purdue account | CERN / FNAL account |
| ------------------------------------------- | -------------- | ------------------- |
| JupyterLab session (CPU / RAM / GPU)        | ✅             | ✅                  |
| Dask Gateway with Kubernetes backend        | ✅             | ✅                  |
| Dask Gateway with Slurm backend             | ✅             | ❌                  |
| Slurm batch submission (Hammer cluster)     | ✅             | ❌                  |
| Write access to Depot (`/depot/cms/`)       | ✅             | ❌ (read-only)      |
| Write access to `/work/` storage            | ✅             | ✅                  |
| Grid directory at Purdue EOS (`/store/user/`) | ✅ (on request) | ❌                  |
