
Writing on EOS
================================

**Using gfal:**
 | gfal command is documented in `Tier2 Purdue CMS site <https://www.physics.purdue.edu/Tier2/user-info/tutorials/dfs_commands.php>`_ . In order to use gfal on the facility, you should first apply these commands on your terminal:

.. code-block::

    voms-proxy-init -verify --rfc --voms cms -valid 192:00
    source /cvmfs/oasis.opensciencegrid.org/osg-software/osg-wn-client/current/el8-x86_64/setup.sh

After these commands, gfal command should work.


**Using xrdcp and xrdfs:**
 | This is more straightforward and only requires a valid grid certificate:

.. code-block::

    voms-proxy-init -verify --rfc --voms cms -valid 192:00

Documentation on xrdcp is present in `Tier2 Purdue CMS site <https://www.physics.purdue.edu/Tier2/user-info/tutorials/dfs_commands.php>`_ .
