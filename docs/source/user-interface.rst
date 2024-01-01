User interface
===========================

The Purdue Analysis Facility provides a user with a dedicated Kubernetes pod, which runs JupyterHub
with JupyterLab interface on top of AlmaLinux8 operating system. 

A JupyterLab session persists even if a user logs out or closes the browser tab;
the user can reconnect to the same session later, possibly from another device.
A session can be closed or restarted via the ``Shut Down`` button located in top right corner of the interface.

The sessions that are not closed manually will be closed automatically after **14 days** of inactivity.
This will delete all unsaved progress, but the user files in the ``/home/<username>/`` and ``/work/`` directorieswill be preserved.
The user data in ``/home/`` and ``/work/`` directories will be cleaned after **6 months** of user inactivity, unless requested otherwise.



The Analysis Facility interface features the following functionality:
..
* Interactive file browser

  * Based at user's dedicated home directory with persistent 25GB storage
  * Multiple mounted external storage volumes for data access and file sharing
  * Drag-and-drop to move files
  * Copy, rename and other actions available via right-click menu

* Jupyter Notebooks with Python3 and ROOT C++ kernels
* Interactive text editor
* Git extension for interactive work with GitHub or GitLab repositories
* Switch between light and dark themes