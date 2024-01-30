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

.. tip::
   
   If for any reason the session creation fails but you need urgent access to your files,
   use ``Minimal JupyterLab interface`` option.

3. Review storage volumes
--------------------------

Work in progress

4. Review kernels and conda environments
-----------------------------------------

Work in progress

5. Set up GitHub account
---------------------------

Work in progress

6. Set up VOMS proxy
----------------------

In order to access data via XRootD, you will need a VOMS certificate.
To obtain and install your CMS VOMS certificate, follow the instructions at
`CMS TWiki <https://twiki.cern.ch/twiki/bin/view/CMSPublic/WorkBookStartingGrid>`_,
specifically section **"Obtaining and installing your Certificate"**.


.. admonition:: Uploading files to Purdue AF
   :class: toggle

   There is no ``ssh`` access to Purdue Analysis Facility. In order to upload a VOMS
   certificate or any other file to your ``/home/`` storage at Purdue AF, you can
   do one of the following:

   * Drag-and-drop a file from your local file browser into Purdue AF file browser.
   * **OR** (Purdue users only):
   
   #. Upload the file to your ``/home/`` directory at Hammer cluster:
   
      .. code-block:: shell
      
         scp /local/path/mycert.p12 <username>@hammer.rcac.purdue.edu
   
   #. SSH into Hammer cluster.

      .. code-block:: shell
      
         ssh <username>@hammer.rcac.purdue.edu

   #. Copy the file to your Depot directory where it will be visible from Purdue AF.

      .. code-block:: shell
      
         cp /hammer/path/mycert.p12 /depot/cms/users/<username>/

   #. Open your Purdue AF session and copy the file from Depot:

      .. code-block:: shell
      
         mkdir ~/.globus
         cp /depot/cms/users/<username>/mycert.p12 ~/.globus

Once the certificate is installed, activate the VOMS proxy:

.. code-block:: shell

   voms-proxy-init --rfc --voms cms -valid 192:00
