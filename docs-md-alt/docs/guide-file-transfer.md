# Uploading and downloading files

Below are the different methods to upload and download files between your local
computer and Purdue AF.

## Drag-and-drop

For *single files only*, you can drag-and-drop a file from your local file browser
into the file browser area of the JupyterLab interface, or in the opposite
direction.

## "Upload" button

There is an "upload" icon ( ⬆️ ) at the top of the JupyterLab file browser.
It also works only for single files.

## Download a directory as an archive

Downloading a directory from Purdue AF as an archive is possible from the
right-click menu. There are two options:

* **"Download as an Archive"** — applies to the directory on which you clicked;
* **"Download Current Folder as an Archive"** — applies to the directory currently
  open in the JupyterLab browser.

!!! warning

    Be careful: choosing the wrong option may result in downloading a very large
    file.

## File exchange from the command line

[SSH access to Purdue AF](guide-ssh-access.md) is possible; however, `scp` commands
will **not** work for that address, since the AF SSH server does not support the
SFTP protocol.

In order to move files via `scp`, you need to establish a connection in the
opposite direction: **SSH from Purdue AF into your local machine.**

* **macOS**: [enable "Remote Login"](https://support.apple.com/guide/mac-help/allow-a-remote-computer-to-access-your-mac-mchlp1066/mac).
* **Ubuntu**: [remote SSH access instructions](https://help.ubuntu.com/stable/ubuntu-help/sharing-secure-shell.html.en).
* **Windows**: [remote SSH access instructions](https://learn.microsoft.com/en-us/windows-hardware/manufacture/desktop/factoryos/connect-using-ssh?view=windows-11).

Other operating systems should have a similar setting. Once remote login is enabled
on your computer, run `scp` from an AF terminal, e.g.:

```shell
# from an AF terminal: copy a file from the AF to your local machine
scp myfile.root <local-username>@<your-computer-address>:/path/on/local/machine/
```

## Transferring CMS data files

To copy ROOT files between Purdue AF and other CMS sites (CERN, FNAL, ...), use
the `xrdcp` or `gfal-copy` commands instead — see [Data access](data-access.md).
