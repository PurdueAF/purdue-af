# SSH access to Purdue AF

Although the web-based JupyterLab interface is the recommended way to access
Purdue AF, it is also possible to connect to your session from a terminal via SSH.

!!! note

    SSH connects you **to your running AF session**, not to a separate login node —
    you must start the session in a web browser first. If you would rather work in
    a full local IDE, see [Access via VSCode-based IDEs](guide-ide-connection.md).

## JupyterHub API token

SSH, [local IDEs](guide-ide-connection.md), and
[AI agents on your own machine](guide-agentic-interface.md#connecting-from-your-own-machine)
authenticate to Purdue AF with a JupyterHub API token. To obtain one, open
[https://cms.geddes.rcac.purdue.edu/hub/token](https://cms.geddes.rcac.purdue.edu/hub/token)
(or, in JupyterLab, go to `File → Hub Control Panel` and click the `Token` tab),
then click `Request new API token` and copy the token string.

!!! warning "Treat the token like a password"

    The token gives full control over your AF session — do not share it or
    commit it to a Git repository.

## Instructions

1. **Start an AF session in the normal way**

    In a web browser, [log in to Purdue AF and start a session](https://cms.geddes.rcac.purdue.edu).

2. **Obtain a [JupyterHub API token](#jupyterhub-api-token)** — you will need
   it in the next step.

3. **Log in to your session from an external terminal**

    ```shell
    ssh <username>@jupyterhub-ssh.cms.geddes.rcac.purdue.edu
    ```

    * If you are using a CERN or FNAL account, remember that your username must
      include the `-cern` or `-fnal` suffix, respectively —
      see [Login methods and usernames](login-methods.md).
    * Instead of a password, paste the **token** obtained in the previous step.

4. **(Optional) Set up an alias in `.bashrc`**

    By adding the following line to `~/.bashrc` or `~/.bash_profile` on your local
    machine, you can avoid copy-pasting the token every time. Note: you will need
    the `sshpass` utility installed on the local machine.

    ```shell
    alias purdue-af='sshpass -p <token> ssh <username>@jupyterhub-ssh.cms.geddes.rcac.purdue.edu'
    ```

    Now simply running the `purdue-af` command will immediately connect you to
    Purdue AF, provided that you have already started a session.

## Caveats

!!! tip

    If you encounter any formatting issues while working at Purdue AF via an SSH
    connection (for example, broken lines in the Vim text editor), try running the
    `resize` command.

!!! warning "No SFTP / scp support"

    `scp` to and from this SSH server does not work — see
    [Uploading and downloading files](guide-file-transfer.md) for alternatives.
