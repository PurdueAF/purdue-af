Access via VSCode-based IDEs
==================================================

VSCode is the most popular IDE with a rich set of features and extensions that go
far beyond what is currently possible in the JupyterLab interface. It also supports different
tools that use LLMs and agentic frameworks for code completion, generation and debugging;
some of them available as VSCode extensions (e.g. GitHub Copilot, OpenAI Codex),
and others as standalone IDEs (e.g. Cursor, Antigravity) built on top of VSCode.

Purdue AF allows to connect to your AF session from any VSCode-based IDE and take advantage
of these features.
Please follow the instructions below; you will only need to run steps 1-4 once, and then
you will be able to connect using instructions from step 5, as long as you have an AF session running.

1. Install Remote-SSH extension in your IDE
--------------------------------------------

#. In VSCode/Cursor/Antigravity, click on the Extensions icon in the left sidebar.
#. Search for ``Remote - SSH`` extension by Anysphere and install it.

2. Configure SSH keys on local machine
-----------------------------------------

#. You may already have the SSH key pair generated: look for ``~/.ssh/id_ed25519`` and ``~/.ssh/id_ed25519.pub`` files on your local machine.
#. If you do not have the key pair yet, you can follow the `GitHub SSH key instructions <https://docs.github.com/en/authentication/connecting-to-github-with-ssh/generating-a-new-ssh-key-and-adding-it-to-the-ssh-agent>`_
   and reuse the generated key pair for Purdue AF.


#. Add the following host entry to ``~/.ssh/config`` on your local machine:

   .. code-block:: ssh

      Host PurdueAF
          HostName cms.geddes.rcac.purdue.edu
          User USERNAME
          IdentityFile ~/.ssh/id_ed25519
          IdentitiesOnly yes
          ProxyCommand websocat --binary -H="Authorization: token TOKEN" asyncstdio: wss://%h/user/USERNAME/sshd/

#. Replace ``USERNAME`` in two places with your Purdue AF username:

   * If you are using Purdue account, this is your Purdue Career accountusername.
   * If you are using CERN account, this is your CERN username followed by ``-cern``.
   * If you are using FNAL account, this is your FNAL username followed by ``-fnal``.

You will also need to replace ``TOKEN`` with the JupyterHub token that you will obtain in the next step.

3. Start a Purdue AF session and obtain a JupyterHub token
-----------------------------------------------------------

#. In your web browser, open `Purdue AF <https://cms.geddes.rcac.purdue.edu>`_ and log in.
#. Select CPU, RAM, and GPU resources and start the session.
#. Once the session is started, in the JupyterLab menu, go to ``File -> Hub Control Panel``.
#. Click the ``Token`` tab.
#. Click ``Request new API token``.
#. Copy the token string and use it to replace ``TOKEN`` in the ``~/.ssh/config`` file.

4. Configure SSH access on the Purdue AF side
---------------------------------------------------

This is the last step that we need to enable your AF session to authorize connections from your local machine.

On your **local machine**, run this to copy your public key to the clipboard:

.. code-block:: shell

   cat ~/.ssh/id_ed25519.pub | pbcopy

On the **AF session**, run the following commands one by one. When the ``cat`` command
prompts for input, paste your public key (``Ctrl-V``) and press ``Ctrl-D`` to finish:

.. code-block:: shell

   mkdir -p ~/.ssh
   chmod 700 ~/.ssh
   cat >> ~/.ssh/authorized_keys
   chmod 600 ~/.ssh/authorized_keys


5. Connect from your IDE
-------------------------

#. In your IDE (VSCode, Cursor, etc.), open the command palette (usually ``Ctrl-Shift-P`` or ``Cmd-Shift-P``).
#. Search for ``Remote-SSH: Connect to Host...`` command and select it.
#. Select ``PurdueAF`` from the host list. A new IDE window will open, connected to your AF session.
#. Open a folder on the AF filesystem once the connection is established. It can be any folder in your
   home directory, or other directory you have access to (e.g. ``/depot/cms/users/<username>/``,
   ``/work/users/<username>/``, etc.).
#. Success! Now you can use your local IDE to browse and edit files in the AF. LLM-powered
   tools will also have access to your remote files, so you can use them to generate and debug code,
   but always be careful not to let them run dangerous commands, e.g. deleting important files.

6. Install extensions on the AF side (optional)
------------------------------------------------

After the connection succeeds, you can install extensions on the remote VSCode server that
now runs on the AF side. To do that, simply open the Extensions tab in the window connected to the AF.

.. warning::

  The Jupyter extension which allows to run remote notebooks from your local IDE is not going
  to work, as it cannot properly discover the AF kernels just yet. We are working on this
  functionality and will announce it when it is available.

Troubleshooting
----------------

If connection fails, you can usually extract useful informantion from the IDE console.

Some known caveats are:

* Your AF home directory must NOT be group-writable for SSH keys to work.
  Check it with ``ls -ld /home/<username>/`` and make sure the group permissions do not
  include ``w``.

* If you see errors like ``websocat: command not found``, check that ``websocat`` command is available on your
  local machine: ``which websocat``. If it is not available, you need to install websocat; if
  it is available but you are still seeing the same error, change the ``ProxyCommand`` in the ``~/.ssh/config`` file
  to use the full path to the websocat binary (for example ``/opt/homebrew/bin/websocat``).