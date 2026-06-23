# Access via VSCode-based IDEs

VSCode is the most popular IDE, with a rich set of features and extensions that go
far beyond what is currently possible in the JupyterLab interface. It also supports
different tools that use LLMs and agentic frameworks for code completion, generation
and debugging; some of them are available as VSCode extensions (e.g. GitHub Copilot,
OpenAI Codex), and others as standalone IDEs (e.g. Cursor, Antigravity) built on top
of VSCode.

Purdue AF allows you to connect to your AF session from any VSCode-based IDE and
take advantage of these features. Please follow the instructions below; you will
only need to run steps 1–6 once, and afterwards you can connect using step 7 alone,
as long as you have an AF session running.

## 1. Install the Remote-SSH extension in your IDE

1. In VSCode/Cursor/Antigravity, click on the Extensions icon in the left sidebar.
2. Search for the `Remote - SSH` extension by Microsoft or Anysphere and install it.

## 2. Install the `websocat` command on your local machine

1. Check if you already have the `websocat` command on your local machine:
   `which websocat`. If the output of this command is not empty, skip this step.
2. Install `websocat`:

    On Linux, run the following commands:

    ```shell
    sudo wget -qO /usr/local/bin/websocat https://github.com/vi/websocat/releases/latest/download/websocat.x86_64-unknown-linux-musl
    sudo chmod a+x /usr/local/bin/websocat
    ```

    On macOS, run the following command:

    ```shell
    brew install websocat
    ```

    Once installed, check that the command is available: `which websocat`.

    On Windows:

    * Download the pre-built binary from
      <https://github.com/vi/websocat/releases/latest/download/websocat.x86_64-pc-windows-msvc.exe>
    * Move the binary to any directory you can access from the command line:
      `C:\path\to\websocat.exe`

## 3. Configure SSH keys on the local machine

1. You may already have an SSH key pair generated: look for the
   `~/.ssh/id_ed25519` and `~/.ssh/id_ed25519.pub` files on your local machine.
2. If you do not have the key pair yet, you can follow the
   [GitHub SSH key instructions](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/generating-a-new-ssh-key-and-adding-it-to-the-ssh-agent)
   and reuse the generated key pair for Purdue AF.
3. Add the following host entry to `~/.ssh/config` on your local machine:

    ```
    Host PurdueAF
        HostName cms.geddes.rcac.purdue.edu
        User USERNAME
        IdentityFile ~/.ssh/id_ed25519
        IdentitiesOnly yes
        ProxyCommand websocat --binary -H="Authorization: token TOKEN" asyncstdio: wss://%h/user/USERNAME/sshd/
    ```

4. If you are on Windows, make two small changes to the entry above:

    * replace `websocat` with the full path to `websocat.exe` from the previous step;
    * replace `asyncstdio:` with `stdio:`.

5. Replace `USERNAME` (**in two places!**) with your Purdue AF username:

    * If you are using a Purdue account, this is your Purdue career account username.
    * If you are using a CERN account, this is your CERN username followed by `-cern`.
    * If you are using an FNAL account, this is your FNAL username followed by `-fnal`.

You will also need to replace `TOKEN` with the JupyterHub token that you will
obtain in the next step.

## 4. Start a Purdue AF session and obtain a JupyterHub token

1. In your web browser, open [Purdue AF](https://cms.geddes.rcac.purdue.edu) and log in.
2. Select CPU, RAM, and GPU resources and start the session.
3. Once the session is started, in the JupyterLab menu go to
   `File → Hub Control Panel`.
4. Click the `Token` tab.
5. Click `Request new API token`.
6. Copy the token string and use it to replace `TOKEN` in the `~/.ssh/config` file.

## 5. Configure SSH access on the Purdue AF side

This is the last step needed to allow your AF session to authorize connections from
your local machine.

On your **local machine**, run this to copy your public key to the clipboard:

On Linux:

```shell
cat ~/.ssh/id_ed25519.pub | xclip -selection clipboard
```

On macOS:

```shell
cat ~/.ssh/id_ed25519.pub | pbcopy
```

On Windows:

```shell
type %USERPROFILE%\.ssh\id_ed25519.pub | clip
- OR, if using Git Bash or other shell -
cat ~/.ssh/id_ed25519.pub | clip
```

In the **AF session**, run the following commands one by one. When the `cat`
command prompts for input, paste your public key (`Ctrl-V`) and press `Ctrl-D` to
finish:

```shell
mkdir -p ~/.ssh
chmod 700 ~/.ssh
cat >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

## 6. Verify home directory permissions on the AF side

Your home directory must have exactly the following permissions:

```shell
$ ls -ld ~/
drwxr-xr-x <username> ... ... ... /home/<username>/
```

If the permissions are different, run `chmod 755 ~/` to fix them.

## 7. Connect from your IDE

1. In your IDE (VSCode, Cursor, etc.), open the command palette
   (usually `Ctrl-Shift-P` or `Cmd-Shift-P`).
2. Search for the `Remote-SSH: Connect to Host...` command and select it.
3. Select `PurdueAF` from the host list. A new IDE window will open, connected to
   your AF session.
4. Once the connection is established, open a folder on the AF filesystem. It can
   be any folder in your home directory, or another directory you have access to
   (e.g. `/depot/cms/users/<username>/`, `/work/users/<username>/`, etc.).
5. Success! Now you can use your local IDE to browse and edit files in the AF.
   LLM-powered tools will also have access to your remote files, so you can use
   them to generate and debug code — but always be careful not to let them run
   dangerous commands, e.g. deleting important files.

## 8. Install extensions on the AF side (optional)

After the connection succeeds, you can install extensions on the remote VSCode
server that now runs on the AF side. To do that, simply open the Extensions tab in
the IDE window connected to the AF.

!!! warning

    The Jupyter extension, which allows running remote notebooks from your local
    IDE, is not going to work yet, as it cannot properly discover the AF kernels.
    We are working on this functionality and will announce it when it is available.

## Troubleshooting

If the connection fails, you can usually extract useful information from the IDE
console. Some known caveats:

* Your AF home directory must NOT be group-writable for SSH keys to work.
  Check it with `ls -ld /home/<username>/` and make sure the group permissions do
  not include `w`.
* If you see errors like `websocat: command not found`, check that the `websocat`
  command is available on your local machine: `which websocat`. If it is not
  available, you need to install websocat; if it is available but you are still
  seeing the same error, change the `ProxyCommand` in the `~/.ssh/config` file to
  use the full path to the websocat binary (for example,
  `/opt/homebrew/bin/websocat`).
* If the connection stopped working after you restarted your session, your token
  may have expired — request a new token (step 4) and update `~/.ssh/config`.
