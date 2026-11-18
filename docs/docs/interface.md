# Interfaces

## User interfaces

Purdue AF provides several user interfaces for analysis development:

* **JupyterLab** — interactive web interface for notebooks, terminals, file
  browsing, and more. This is the default interface when starting a session.
* **Web-based Visual Studio Code (code-server)** — to open it, either select the 
  VSCode interface when creating a session, or click "Open VSCode" button at top
  right of the JupyterLab interface.
* [Connection from local VSCode-based IDEs](guide-ide-connection.md) (VSCode, Cursor, etc.)
* [SSH connection from a local terminal](guide-ssh-access.md)
* [AI agents](agents.md) — Claude Code, Codex and OpenCode run in every
  session's terminal; Claude Code and Codex also as VS Code extensions, and all
  three in a chat panel in JupyterLab. An agent on your own machine
  [connects with a token](guide-agentic-interface.md).

Notebooks run on Jupyter kernels made from Pixi or Conda environments — see
[Software stacks](software.md).

## Uploading and downloading files

* Drag a file between your computer's file browser and JupyterLab's, or use the
  upload button (⬆) at the top of JupyterLab's file browser. Both take one file
  at a time.
* To download a directory, right-click it in JupyterLab's file browser and
  choose **Download as an Archive**. **Download Current Folder as an Archive**
  packs the folder that is open instead, which can be very large.
* `scp` works only from the AF to your computer — see
  [SSH access](guide-ssh-access.md#caveats).
* CMS data files move between sites with `xrdcp` or `gfal-copy` — see
  [Data access](data-access.md).
