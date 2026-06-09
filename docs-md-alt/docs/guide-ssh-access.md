# SSH access to Purdue AF

Although the web-based JupyterLab interface is the recommended way to access
Purdue AF, it is also possible to connect to your session from a terminal via SSH.

!!! note

    SSH connects you **to your running AF session**, not to a separate login node —
    you must start the session in a web browser first. If you would rather work in
    a full local IDE, see [Access via VSCode-based IDEs](guide-ide-connection.md).

## Instructions

1. **Start an AF session in the normal way**

    In a web browser, [log in to Purdue AF and start a session](https://cms.geddes.rcac.purdue.edu).

2. **Obtain an authentication token**

    1. Go to `File → Hub Control Panel`.
    2. Click the `Token` tab in the top left of the page.
    3. Click `Request new API token` to obtain the token string — you will need it
       in the next step.

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

    At the moment, the SSH server at Purdue AF does not support the SFTP protocol,
    which means that you cannot use `scp` commands to download and upload files
    from your local machine.

    Use the web interface to transfer files instead — see
    [Uploading and downloading files](guide-file-transfer.md):

    * upload files by drag-and-dropping them into the Jupyter file browser, or via
      the "Upload" button at the top of the file browser window;
    * download single files via the right-click menu;
    * download a directory by right-clicking on it and selecting
      "Download as an Archive".
