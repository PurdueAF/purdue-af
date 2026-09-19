# Writing to EOS

The Purdue EOS storage is mounted **read-only** at `/eos/purdue/`. To write files
to EOS (for example, to your Grid directory `/store/user/<cern-username>/`), use
the `gfal` or `xrdcp`/`xrdfs` commands.

A typical workflow is: save job outputs into `/tmp/<username>/` (on the session,
Slurm job, or Dask worker that produced them), then copy them to EOS — see
[Storage volumes](storage.md) for why this is preferred over writing to Depot from
many jobs at once.

## Using xrdcp and xrdfs

This option only requires a valid
[VOMS proxy](getting-started.md#6-set-up-a-voms-proxy). For example:

```shell
# copy a local file to your Grid directory on Purdue EOS
xrdcp /tmp/$USER/output.root root://eos.cms.rcac.purdue.edu//store/user/<cern-username>/output.root

# list the contents of a directory
xrdfs root://eos.cms.rcac.purdue.edu ls /store/user/<cern-username>/
```

Documentation on `xrdcp` is available at the
[Purdue Tier-2 CMS site](https://www.physics.purdue.edu/Tier2/user-info/tutorials/dfs_commands.php).

## Using gfal

The `gfal` commands are documented at the
[Purdue Tier-2 CMS site](https://www.physics.purdue.edu/Tier2/user-info/tutorials/dfs_commands.php).
They are available in every terminal and also require a valid
[VOMS proxy](getting-started.md#6-set-up-a-voms-proxy). For example:

```shell
gfal-copy /tmp/$USER/output.root root://eos.cms.rcac.purdue.edu//store/user/<cern-username>/output.root
```

!!! note "See also"

    * [Data access](data-access.md) — copying files from other CMS sites
    * [Storage volumes](storage.md) — where CRAB and Rucio outputs end up
