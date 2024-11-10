Getting started
======================================

Welcome to the Purdue Analysis Facility!

This guide will help you to quickly set up the work environment for your analysis.

|login_to_af|

.. |login_to_af| raw:: html

   <a href="https://cms.geddes.rcac.purdue.edu/hub" target="_blank">
      🚀 Login to Purdue Analysis Facility
   </a>

1. Choose a login method
------------------------

* Purdue University account - recommended if you are a Purdue user
* CERN account (CMS users only)
* FNAL account

2. Select resources
------------------------

After a successful login, you will be redirected to a page
where you can select the number of CPU cores, RAM, and GPUs for your session.

The default values are enough to get started; if more resources are needed,
you can close the session (``Shut Down`` button in top right corner) and
recreate it with a different selection.

.. important::

   There are two options for GPU selection:

   * 5GB "slice" of Nvidia A100 GPU - immediately available, but less powerful
   * Full 40GB instance of Nvidia A100 GPU - more powerful, but subject to availability

   :doc:`Learn more about GPU access at Purdue AF <doc-gpus>`

.. tip::
   
   If for any reason the session creation fails but you need urgent access to your files,
   use ``Minimal JupyterLab interface`` option.

3. Review storage volumes
--------------------------

After the session has started, review the available storage options:

* The default directory in file browser and Terminal is ``/home/<username>``, it has 25 GB quota.
* In the file browser you will see symlinks to the following directories:

  * ``work`` (also mounted at ``/work/``) - shared storage for AF users.
  
    There are 100GB personal directories under ``work/users``, and project directories under ``work/projects``.
  * ``depot`` (also mounted at ``/depot/cms``) - shared storage **only for Purdue users**.
    
    Any code that uses SLURM or Dask Gateway should be stored here.
  * ``eos-purdue`` (also mounted at ``/eos/purdue``) - **read-only** directory that stores large datasets and users'
    Grid directories.
  
.. seealso::

   * Detailed description of storage options: :doc:`doc-storage`.
   * :doc:`guide-cern-eos`

4. Review kernels and Conda envs
-----------------------------------------

There are two pre-installed Python3 kernels that include all of the most common
packages used in HEP analyses (see :doc:`full list of packages <doc-software>`).
The "default" Python3 kernel will be automatically selected when you create
a new Jupyter notebook.

When working in a Terminal instead of a Jupyter Notebook,
you need to activate the environment explicitly, e.g.:

.. code-block:: shell

   conda activate /depot/cms/kernels/python3

If you need a package that is missing from the pre-installed kernels, please
:doc:`contact Purdue AF support <doc-support>`.

You can also :doc:`create and share custom kernels <guide-conda>`.

5. Set up GitHub access
---------------------------

Follow these instructions:

* |generate-ssh-key|
* |add-ssh-key|

.. |generate-ssh-key| raw:: html

   <a href="https://docs.github.com/en/authentication/connecting-to-github-with-ssh/generating-a-new-ssh-key-and-adding-it-to-the-ssh-agent" target="_blank">
      Generating a new SSH key and adding it to the ssh-agent
   </a>

.. |add-ssh-key| raw:: html

   <a href="https://docs.github.com/en/authentication/connecting-to-github-with-ssh/adding-a-new-ssh-key-to-your-github-account" target="_blank">
      Adding a new SSH key to your GitHub account
   </a>


After you have generated an SSH key and added it to your GitHub account, run the
following command in a Terminal to finish GitHub authentication:

.. code-block:: shell

   ssh -T git@github.com

6. Set up VOMS proxy
----------------------

#. In order to access data via XRootD, you will need a VOMS certificate.
   To obtain and install your CMS VOMS certificate, follow the instructions at
   `CMS TWiki <https://twiki.cern.ch/twiki/bin/view/CMSPublic/WorkBookStartingGrid>`_,
   specifically the section **"Obtaining and installing your Certificate"**.

   .. admonition:: Uploading files to Purdue AF
      :class: toggle

      To upload files to Purdue AF, you can either:

      - Drag-and-drop a file from local directory into the Jupyter file browser, OR
      - click "upload" icon (upward arrow) at the top of the Jupyter file browser
        and select a file to upload.

   .. .. admonition:: Uploading files to Purdue AF
   ..    :class: toggle

   ..    There is no ``ssh`` access to Purdue Analysis Facility. In order to upload a VOMS
   ..    certificate or any other file to your ``/home/`` storage at Purdue AF, you can
   ..    do one of the following:

   ..    *  Drag-and-drop a file from your local file browser into Purdue AF file browser.
   ..    *  **OR** (Purdue users only):
      
   ..       #. Upload the file from your computer to the ``/home/`` directory at Hammer cluster:
         
   ..          .. code-block:: shell
            
   ..             scp /local/path/mycert.p12 <username>@hammer.rcac.purdue.edu
         
   ..       #. SSH into Hammer cluster:

   ..          .. code-block:: shell
            
   ..             ssh <username>@hammer.rcac.purdue.edu

   ..       #. Copy the file to your Depot directory where it will be visible from Purdue AF:

   ..          .. code-block:: shell
            
   ..             cp /hammer/path/mycert.p12 /depot/cms/users/<username>/

   ..       #. Open your Purdue AF session and copy the file from Depot:

   ..          .. code-block:: shell
            
   ..             mkdir ~/.globus
   ..             cp /depot/cms/users/<username>/mycert.p12 ~/.globus

#. (Optional) Specify the path where your VOMS proxy will be stored. If you are
   using SLURM or Dask Gateway, the proxy location must be on Depot
   (currently only allowed for users with Purdue account):

   .. code-block:: shell

      export X509_USER_PROXY=/depot/cms/users/$USER/x509up_u$NB_UID


#. Activate the VOMS proxy:

   .. code-block::

      voms-proxy-init --rfc --voms cms -valid 192:00

7. Subscribe to Purdue AF mailing list
----------------------------------------

:doc:`Instructions to subsrcibe to the mailing list <doc-support>`.

.. warning:: 

   Currently only possible for users with Purdue email accounts.

