* The Rucio client is installed in the Purdue Analysis Facility, and users can create rules following
the standard `procedures <https://twiki.cern.ch/twiki/bin/view/CMSPublic/RucioUserDocsRules>`_.
* For example, open a Terminal, and do the initial setup:
    
.. code-block:: shell
    source /cvmfs/cms.cern.ch/cmsset_default.sh
    source /cvmfs/cms.cern.ch/rucio/setup-py3.sh
    voms-proxy-init -voms cms -rfc -valid 192:00
    export RUCIO_ACCOUNT=<your-CERN-account>
    
* Then check that Rucio recognizes you:
    
.. code-block:: shell
    rucio whoami
    
* and that you can list your already existing rules:
    
.. code-block:: shell
    rucio list-rules --account <your-CERN-account>
    
* Local Purdue users can create rules for replicating datasets at Purdue.
The requests will follow the usual approval workflow, and after completion
the datasets will be available in our EOS storage.