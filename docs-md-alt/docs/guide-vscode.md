# VSCode integration via the JupyterHub extension

![](https://upload.wikimedia.org/wikipedia/commons/thumb/9/9a/Visual_Studio_Code_1.35_icon.svg/240px-Visual_Studio_Code_1.35_icon.svg.png){ width="30" align=left }

[Visual Studio Code](https://code.visualstudio.com) is currently the most popular
integrated development environment (IDE).

This guide describes how to run **local notebooks in VSCode against remote Purdue
AF kernels** using the JupyterHub extension. If you instead want to open the AF
filesystem directly in your IDE (with full terminal access and LLM-powered tools),
see [Access via VSCode-based IDEs](guide-ide-connection.md).

## Purdue AF + VSCode use cases

* Running local notebooks with remote Pixi/Conda environments (Jupyter kernels),
  which ensures that you use exactly the same versions of packages without
  reinstalling the environment locally.
* Scaling out from your computer to hundreds of cores using
  [Dask Gateway](guide-dask-gateway.md) instances at Purdue AF.

<figure markdown="span">
  ![](images/vscode.png){ width="90%" }
</figure>

## Installation instructions

1. **Install VSCode and the JupyterHub extension**

    1. [Install VSCode](https://code.visualstudio.com).
    2. Open the "Extensions" panel in the VSCode sidebar.
    3. Search for the `JupyterHub` extension and install it.

2. **Create or open a Jupyter notebook**

    1. Open a local Jupyter notebook that you want to use with Purdue AF, or create
       a new notebook.
    2. You may need to install the `Jupyter` extension for VSCode for a better
       experience.

3. **Obtain an authentication token for your AF session**

    1. In a web browser, [log in to Purdue AF and start a session](https://cms.geddes.rcac.purdue.edu).
    2. Go to `File → Hub Control Panel`.
    3. Click the `Token` tab in the top left of the page.
    4. Click `Request new API token` to obtain the token string — you will need it
       in the next step.

4. **Connect your notebook to the AF session**

    1. Switch back to the notebook opened in VSCode.
    2. In the top right corner of the notebook, click the `Select kernel` button,
       which will open the Command Palette.
    3. In the Command Palette, select `Existing JupyterHub Server`.
    4. When prompted for the URL of the server, paste
       `https://cms.geddes.rcac.purdue.edu`.
    5. When prompted for a username:

        * if you are using a Purdue account, type your Purdue username;
        * if you are using a CERN account, type your CERN username followed by `-cern`;
        * if you are using a Fermilab account, type your FNAL username followed by `-fnal`.

    6. When prompted for a token or password, paste the token obtained in step 3.
    7. Type any name (e.g. "Purdue AF") to save the JupyterHub server setup for the
       future.

5. **Select a kernel**

    1. Once the setup is complete, you will be able to choose from the Purdue AF
       kernels, including the default Python kernels, as well as any custom
       environments that you normally have access to.
    2. To change the kernel in the notebook, simply click `Select Kernel` in the
       top right corner, and choose from recently used kernels, or click
       `Select Another Kernel` and then `Existing JupyterHub Server`.
       You will not need to repeat steps 4.4–4.7.

6. **(Optional) Start a Dask Gateway cluster**

    Follow the
    [instructions for starting a Dask Gateway cluster from a Jupyter notebook](guide-dask-gateway.md) —
    they will work in your local notebook in VSCode too, but interactive widgets
    will not be displayed.
