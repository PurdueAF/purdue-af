Accelerating RooFit with GPUs
================================

Fitting data distributions with analytical models is a common task in CMS analyses.
Depending on the size of the dataset and the number of free parameters in the model,
the fitting can take a significant amount of time and become a bottleneck in the analysis.

The standard tool to perform such fits is RooFit, distributed with the ROOT framework.
Since ROOT version 6.26, RooFit supports GPU-accelerated fitting using CUDA backend,
which allows to speed up the fitting process by up to an order of magnitude.

Purdue Analysis Facility now supports this feature, allowing users to
leverage available GPU resources to speed up their RooFit code. The feature is
supported in both Jupyter Notebooks and Terminals, and for both C++ ROOT interface and PyROOT.


Pre-requisites
~~~~~~~~~~~~~~~

1. Start your AF session with a GPU.
2. Load the LCG view with CUDA-enabled ROOT build.
   `LCG "releases" and "views" <https://lcgdocs.web.cern.ch/lcgdocs/lcgreleases/introduction/>`_
   are software stacks distributed by CERN.

   a. If using a Jupyter Notebook: simply select the ``LCG_106b_cuda`` kernel.
   b. If using a Terminal, run the following command:

      .. code-block:: shell

         source /cvmfs/sft.cern.ch/lcg/views/LCG_106b/x86_64-el8-gcc11-opt/setup.sh

.. warning::

   The CUDA-enabled ROOT build is currently available only via the LCG software stack.
   It is not available in other kernels such as the "default" Python3 kernel or ``coffea-latest``.

   The only supported ROOT version at the moment is ``6.32.08``.

Enabling CUDA backend in RooFit
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

To enable CUDA backend in RooFit, the only thing you need to do is to
pass the ``rt.RooFit.EvalBackend.Cuda()`` argument to ``fitTo()`` command in RooFit.

Below is an example of the code that uses CUDA backend for fitting a Z-boson mass spectrum.

.. code-block:: python

   import ROOT as rt

   inputfile = "workspace_ggh_All_Zfit_no_e_cut_UL_calib_cat5.root"

   rt.EnableImplicitMT()

   file = rt.TFile.Open(inputfile)

   canvas = rt.TCanvas()
   canvas.cd()

   mass =  rt.RooRealVar("mh_ggh","mass (GeV)",100,85,99)
   frame = mass.frame()

   # Breit Wigner
   bwWidth = rt.RooRealVar("bwz_Width" , "widthZ", 2.5, 0, 30)
   bwmZ = rt.RooRealVar("bwz_mZ" , "mZ", 91.2, 90, 92)
   sigma = rt.RooRealVar("sigma" , "sigma", 2, 0.0, 5.0)
   bwWidth.setConstant(True)
   model1_1 = rt.RooBreitWigner("bwz", "BWZ",mass, bwmZ, bwWidth)

   # Double Sided Crystal Ball
   mean = rt.RooRealVar("mean" , "mean", 0, -10, 10) # mean is mean relative to BW
   sigma = rt.RooRealVar("sigma" , "sigma", 2, .2, 4.0)
   alpha1 = rt.RooRealVar("alpha1" , "alpha1", 2, 0.01, 45)
   n1 = rt.RooRealVar("n1" , "n1", 10, 0.01, 185)
   alpha2 = rt.RooRealVar("alpha2" , "alpha2", 2.0, 0.01, 65)
   n2 = rt.RooRealVar("n2" , "n2", 25, 0.01, 385)
   model1_2 = rt.RooCrystalBall("dcb","dcb",mass, mean, sigma, alpha1, n1, alpha2, n2)

   mass.setBins(10000,"cache") # cache is repre of the varibale only used in FFT
   mass.setBins(200) # bin to 100 bins otherwise, fitting with FFT conv is gonna take forever
   mass.setMin("cache",50.5) 
   mass.setMax("cache",130.5)
   model1 = rt.RooFFTConvPdf("BWxDCB", "BWxDCB", mass, model1_1, model1_2)

   # Exponential background
   coeff = rt.RooRealVar("coeff", "coeff", 0.001, 0.000001, 1.0)
   shift = rt.RooRealVar("shift", "Offset", 91, 75, 105)
   shifted_mass = rt.RooFormulaVar("shifted_mass", "@0-@1", rt.RooArgList(mass, shift))
   model2 = rt.RooExponential("bkg", "bkg", shifted_mass, coeff)

   sigfrac = rt.RooRealVar("sigfrac", "sigfrac", 0.99, 0, 1.0)
   model = rt.RooAddPdf("model3", "model3", [model1, model2],sigfrac)

   data = file.w.data("data_Zfit_no_e_cut_UL_calib_cat5")

   model.fitTo(data, rt.RooFit.Save(), rt.RooFit.EvalBackend.Cuda()) #GPU

   data.plotOn(frame)
   model.plotOn(frame, rt.RooFit.LineColor(rt.kRed))
   model.plotOn(frame, rt.RooFit.Components("BWxDCB"),rt.RooFit.LineColor(rt.kBlue))
   model.plotOn(frame, rt.RooFit.Components("bkg"),rt.RooFit.LineColor(rt.kGreen))


   frame.Draw()
   canvas.Update()
   canvas.Draw()

To run this code, you can download the input file
``workspace_ggh_All_Zfit_no_e_cut_UL_calib_cat5.root``
from `https://cernbox.cern.ch/s/zKjJHZxRbDkADPf <https://cernbox.cern.ch/s/zKjJHZxRbDkADPf>`_.
