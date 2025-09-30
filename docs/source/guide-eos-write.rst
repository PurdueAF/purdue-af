
Writing on EOS
================================

Use gfal command documented in `Tier2 Purdue CMS site <https://www.physics.purdue.edu/Tier2/user-info/tutorials/dfs_commands.php>`_ . In order to use gfal on the facility, you should first:

.. code-block::
    voms-proxy-init -verify --rfc --voms cms -valid 192:00
    source /cvmfs/oasis.opensciencegrid.org/osg-software/osg-wn-client/current/el8-x86_64/setup.sh

After this command, gfal command should work.