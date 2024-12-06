
Upload and download files from local PC
========================================

Below are different methods to upload and download files to/from Purdue AF.

Drag-and-drop
~~~~~~~~~~~~~~~

For `single files only`, you can drag-and-drop a file from local file
browser into the file browser area of JupyterLab interface, or in the
opposite direction.

"Upload" button
~~~~~~~~~~~~~~~~~

There is an "upload" icon ( ⬆️ ) at the top of the JupyterLab browser.
It also works only for single files.

Download as an archive
~~~~~~~~~~~~~~~~~~~~~~~

Downloading a directory from Purdue AF as an archive is possible from the
right-click menu. There are two options:

- "Download as an Archive" - applies to the directory on which you clicked;
- "Download Current Folder as an Archive" - applies to the directory open
  in the JupyterLab browser.

Be careful: choosing the wrong option may result in downloading a very large
file.

File exchange from command line
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

SSH access to Purdue AF is possible: :doc:`<guide-ssh-access>`, however,
``scp`` commands will not work for this address.

In order to move files via ``scp``, you need to establish a
connection in the opposite direction:
**SSH from Purdue AF into your local machine.**

- **macOS**: `Enable "Remote Login" <https://support.apple.com/guide/mac-help/allow-a-remote-computer-to-access-your-mac-mchlp1066/mac>`_.
- **Ubuntu:** `Remote SSH access instructions <https://help.ubuntu.com/stable/ubuntu-help/sharing-secure-shell.html.en>`_.
- **Windows:** `Remote SSH access instructions <https://learn.microsoft.com/en-us/windows-hardware/manufacture/desktop/factoryos/connect-using-ssh?view=windows-11>`.
  
Other operating systems should have a similar setting.