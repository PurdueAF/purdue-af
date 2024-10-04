
VSCode integration
================================

.. image:: https://upload.wikimedia.org/wikipedia/commons/thumb/9/9a/Visual_Studio_Code_1.35_icon.svg/240px-Visual_Studio_Code_1.35_icon.svg.png
   :width: 30
   :align: left

`Visual Studio Code <https://code.visualstudio.com>`_ is currenlty the most popular
integrated development environment (IDE).

Purdue AF + VSCode use cases
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

- Running local notebooks with remote Conda environments (Jupyter kernels), which ensures that
  you use exactly the same versions of packages without a need to reinstall the environment.
- Scaling out from you computer to 100s cores using :doc:`Dask Gateway <guide-dask-gateway>` instances at Purdue AF.

.. image:: images/vscode.png
   :width: 90%
   :align: center


Installation instructions
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

#. **Install VSCode and JupyterHub extension**

   #. `Install VSCode <https://code.visualstudio.com>`_.
   #. Open the "Extensions" panel in VSCode sidebar.
   #. Search for ``JupyterHub`` extension and install it.

#. **Create or open a Jupyter notebook**

   #. Open a local Jupyter notebook that you want to use with Purdue AF, or create a new notebook.
   #. You may need to install ``Jupyter`` extension for VSCode for better experience.

#. **Obtain authentication token for your AF session**

   #. In a web browser, `login to Purdue AF and start a session <https://cms.geddes.rcac.purdue.edu>`_.
   #. Go to ``File -> Hub Control Panel``.
   #. Click ``Token`` tab in top left of the page.
   #. Click ``Request new API token`` to obtain the token string - you will need it in the next step.

#. **Connect your notebook to AF session**

   #. Switch back to the notebook opened in VSCode.
   #. In the top right corner of the notebook, click ``Select kernel`` button, which will open Command Palette.
   #. In the Command Palette, select ``Existing JupyterHub Server``.
   #. When prompted for URL of the server, paste ``https://cms.geddes.rcac.purdue.edu``.
   #. When prompted for username:

      * If you are using Purdue account, type your Purdue username.
      * If you are using CERN account, type your CERN username followed by ``-cern``.
      * If you are using Fermilab account, type your FNAL account followed by ``-fnal``.

   #. When prompted for token or password, paste the token obtained in step 3.
   #. Type any name (e.g. "Purdue AF") to save the JupyterHub server setup for future.

#. **Select kernel**

   #. Once setup is complete, you will be able to choose from the Purdue AF kernels, including default Python kernels, as well as any custom Conda environments that you normally have access to.
   #. To change kernel in the notebook, simply click on ``Select Kernel`` in top right corner, and choose from recently used kernels or click ``Select Another Kernel`` and then ``Existing JupyterHub Server``. You will not need to repeat steps 4.4 - 4.7.

#. (optional) **Start a Dask Gateway cluster**

   Follow :doc:`instructions to start Dask Gateway cluster from a Jupyter notebook <guide-dask-gateway>` -
   they will work in your local notebook in VSCode too, but interactive widgets will
   not be displayed.
