Private MC generation
======================

🏗️ Work in progress 🚧

This guide describes private Mc production.
Comprehensive documentation about central MC production in CMS can be found here:
`<https://cms-pdmv.gitbook.io>`_.


.. image:: images/mc_gen.png
   :width: 80%
   :align: center

We assume that we use MadGraph as our generator. 
In MadGraph, we have different cards to define a process. 
Link to short MG tutorial : https://twiki.cern.ch/twiki/bin/view/CMSPublic/MadgraphTutorial

In this example below : We are going to produce DY (pp → ll) samples.
We define this process in MadGraph and it creates LHE files
(python file with settings).

Here, we are going to use UL18 DY LHE file already produced by cms ppd.


Step 1 : LHE, GEN, SIM production
------------------------------------

For this step, we will use the ``CMSSW_10_6_30`` release. 

.. code-block:: shell

   mkdir samples_production
   cd samples_production

   curl -s -k https://cms-pdmv-prod.web.cern.ch/mcm/public/restapi/requests/get_fragment/TAU-RunIISummer20UL18wmLHEGEN-00001 --retry 3 --create-dirs -o Configuration/GenProduction/python/TAU-RunIISummer20UL18wmLHEGEN-00001-fragment.py 
   [ -s Configuration/GenProduction/python/TAU-RunIISummer20UL18wmLHEGEN-00001-fragment.py ] || exit $?;

   export SCRAM_ARCH=slc7_amd64_gcc700
   source /cvmfs/cms.cern.ch/cmsset_default.sh
   voms-proxy-init -voms cms

   cmsrel CMSSW_10_6_17_patch1
   cd CMSSW_10_6_17_patch1/src

   eval `scram runtime -sh`
   mv ../../Configuration .
   scram b -j8
   cd ../..



For testing purposes, we will only generate 10 events.

To get the configuration file :

.. code-block::

   cmsDriver.py Configuration/GenProduction/python/TAU-RunIISummer20UL18wmLHEGEN-00001-fragment.py --python_filename TAU-RunIISummer20UL18wmLHEGEN-00001_1_cfg.py --eventcontent RAWSIM --customise Configuration/DataProcessing/Utils.addMonitoring --datatier GEN-SIM --fileout file:TAU-RunIISummer20UL18GS.root --conditions 106X_upgrade2018_realistic_v4 --beamspot Realistic25ns13TeVEarly2018Collision --customise_commands process.source.numberEventsInLuminosityBlock="cms.untracked.uint32(250)" --step LHE,GEN,SIM --geometry DB:Extended --era Run2_2018 --no_exec --mc -n 10


   cmsRun TAU-RunIISummer20UL18wmLHEGEN-00001_1_cfg.py 

Description of arguments: https://cms-pdmv.gitbook.io/project/cmsdriver-argument-and-meaning


This will give a GEN-SIM output file. To produce a required number of events (~1M),
we need to submit a crab job with production. 

GEN-SIM: starts from a Monte Carlo generator, produces events at generator level
(the four vectors of the particles) and simulates the energy released by the
particles in the crossed detectors. Important parameters for such campaigns are:

* Beamspot
* Generator fragment (specifies the process which needs to be generated)
* Detector geometry




Step 2 GEN-SIM to (DIGI, L1, DIGI2RAW)
---------------------------------------


With PU:

.. code-block:: shell

   cmsDriver.py  --python_filename TAU-RunIISummer20UL18DIGI-00007_1_cfg.py --eventcontent RAWSIM --pileup 2018_25ns_UltraLegacy_PoissonOOTPU --customise Configuration/DataProcessing/Utils.addMonitoring --datatier GEN-SIM-DIGI --fileout file:TAU-RunIISummer20UL18DIGI-00007.root --pileup_input "dbs:/MinBias_TuneCP5_13TeV-pythia8/RunIISummer20UL18SIM-106X_upgrade2018_realistic_v11_L1v1-v2/GEN-SIM" --conditions 106X_upgrade2018_realistic_v11_L1v1 --step DIGI,L1,DIGI2RAW --geometry DB:Extended --filein file:TAU-RunIISummer20UL18GS.root  --era Run2_2018 --runUnscheduled --no_exec --mc -n 10

No PU:

.. code-block:: shell

   cmsDriver.py  --python_filename TAU-RunIISummer20UL18DIGI-00007_1_cfg.py --eventcontent RAWSIM --customise Configuration/DataProcessing/Utils.addMonitoring --datatier GEN-SIM-DIGI --fileout file:TAU-RunIISummer20UL18DIGI-00007.root  --conditions 106X_upgrade2018_realistic_v11_L1v1 --step DIGI,L1,DIGI2RAW --geometry DB:Extended --filein file:TAU-RunIISummer20UL18GS.root --era Run2_2018 --runUnscheduled --no_exec --mc -n 10


Output : ``TAU-RunIISummer20UL18DIGI-00007.root``


Step 3:
----------

Adding the HLT objects /information. For these samples:
HLTv32 is added which is present in CMSSW_10_2_16_UL

We will set up CMSSW_10_2_16_UL release for this step.
(We will try a workaround for this).






.. https://gitlab.cern.ch/shjeon/SampleProduction