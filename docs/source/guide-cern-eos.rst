How to enable access to CERN EOS
==================================

Access to `CERNBox <https://cernbox.cern.ch>`_ (CERN EOS storage) can be enabled in the Purdue Analysis Facility
for anyone who has CERN account, regardless of which account they used to log in. 

In order to enable access to user’s CERNBox directory, one should run the ``eos-connect`` command from a Bash
terminal opened inside an Analysis Facility session. This command will trigger Kerberos authentication,
and create a symlink pointing to the user’s CERNBox directory in the file browser.

Example of the ``eos-connect`` command output:

.. code-block::

   [dkondra@purdue-af-1 ~]$ eos-connect
   
   ------------------------ Connecting to CERN EOS ------------------------
   
    > Kerberos ticket not found.
    > Symlink /home/dkondra/eos-cern doesn't exist yet.
   
    > Let's start with initializing the Kerberos ticket.
    > What is your CERN username? Enter below:
    > dkondrat
   Password for dkondrat@CERN.CH: 
    > Creating symlink /home/dkondra/eos-cern that points to /eos/cern/home-d/dkondrat/
   
    > The directory 'eos-cern' should appear in the file browser in a few seconds.
    > The interaction with CERN EOS may be slow at first.
   
    > If the file browser shows 'eos-cern' as a file rather than a directory,
    > try restarting the session (File > Hub Control Panel > Stop My Server),
    > and then run the eos-connect command again.
   --------------------------------- Done! ---------------------------------
   [dkondra@purdue-af-1 ~]$


The Kerberos ticket does not persist between sessions, therefore connection to CERN EOS must be re-established
(by running the ``eos-connect`` command) in each new session where access to CERNBox is desired.

CERN EOS directory can be accessed without ``eos-connect`` command, by simply initializing a
Kerberos ticket via ``kinit`` command. However, in this case the directory will not appear in
the file browser and will be accessible only at ``/eos/cern/``.