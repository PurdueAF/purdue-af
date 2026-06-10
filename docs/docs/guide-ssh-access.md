# SSH access to Purdue AF

Although the web-based JupyterLab interface is the recommended way to access Purdue AF,
it is also possible to connect to your session from a terminal via SSH.

To achieve this:

1. **Start an AF session in a normal way**

    1. In a web browser, [login to Purdue AF and start a session](https://cms.geddes.rcac.purdue.edu).

2. **Obtain an authentication token**

    1. Go to `File -> Hub Control Panel`.
    2. Click `Token` tab in top left of the page.
    3. Click `Request new API token` to obtain the token string - you will need it in the next step.

3. **Log in to your session from external Terminal**

    ```shell
    ssh <username>@jupyterhub-ssh.cms.geddes.rcac.purdue.edu
    ```

    - If you are using CERN or FNAL account, remember that your username should include
      `-cern` or `-fnal` suffix, respectively.
    - Instead of password, paste the **token** obtained in the previous step.

4. **(Optional) Set up an alias in .bashrc**

    By adding the following line to `~/.bashrc` or `~/.bash_profile` on you local machine,
    you can avoid copy-pasting the token every time. Note: you will need to have `sshpass` utility
    installed on the local machine.

    ```shell
    alias purdue-af='sshpass -p <token> ssh <username>@jupyterhub-ssh.cms.geddes.rcac.purdue.edu'
    ```

    Now simply running the `purdue-af` command will immediately connect you to Purdue AF,
    provided that you had already started the session.

!!! tip

    If you encounter any formatting issues while working at Purdue AF via SSH connection
    (for example, broken lines in Vim text editor), try running `resize` command.

!!! warning

    At the moment, the SSH server at Purdue AF does not support SFTP protocol, which means that
    you will not be able to use `scp` commands to download and upload files from your local machine.

    Use web interface to download and upload files:

    - Uploading can be done by drag-and-dropping files to Jupyter file browser, or by using
      "Upload" button at the top of the Jupyter file browser window.
    - Downloading single files can also be done via drag-and-drop
    - To download a directory, right-click on it and select "Download as an Archive"
