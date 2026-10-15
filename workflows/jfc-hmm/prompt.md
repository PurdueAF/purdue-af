# H→µµ with the full CMS Run 2 dataset

Search for the Standard Model Higgs boson decaying to a pair of muons in the
CMS proton-proton collision data of 2016 to 2018 at √s = 13 TeV, in its
Ultra-Legacy reconstruction (about 138 fb⁻¹), through every production mode of
the Higgs boson the data are sensitive to: gluon fusion, vector boson fusion,
and production with a W or Z boson or a top quark pair. Measure the signal
strength μ, the ratio of the observed σ·B(H→µµ) to the Standard Model's, at
m_H = 125.38 GeV, and the observed and expected significance of the signal
against the background-only hypothesis, with every systematic uncertainty the
measurement is subject to.

The published analysis this reproduces is CMS, "Evidence for Higgs boson decay
to a pair of muons", JHEP 01 (2021) 148, arXiv:2009.04363 (CMS-HIG-19-006),
with the legacy reconstruction of the same years: from the 13 TeV data,
μ = 1.19 +0.41/−0.40 (stat) +0.17/−0.16 (syst) (its section 10) and a
significance of 2.95σ (2.46σ expected) (its Table 10). Its HEPData
record, https://www.hepdata.net/record/ins1815813, holds its results per
category and its distributions; a table downloads as
`https://www.hepdata.net/download/table/ins1815813/<table name>/1/yaml`. The
earlier CMS result on 2016 data is arXiv:1807.06325 and the ATLAS result on its
full Run 2 data is arXiv:2007.07830; m_H = 125.38 ± 0.14 GeV is from
arXiv:2002.06398. Download these papers from arXiv into `docs/` first.

This is the analysis at full scale, with nothing simplified: all of the data
and simulation in `datasets.yaml`, every event of them, every production mode,
and the full set of systematic uncertainties.

Data source: `datasets.yaml` lists, for each data-taking period (2016 before
and after the change of the tracker's VFP setting, 2017, 2018), the SingleMuon
data, the H→µµ signal samples and the simulated backgrounds as CMS NanoAOD
datasets, one private production by the directory of its files; the signal
samples' cross sections; and each period's integrated luminosity,
certified-luminosity JSON and single-muon trigger, with their sources. Find a
dataset's files with DAS and read them through XCache.

Resources: process the data on Dask Gateway with as many cores as help, up to
1000. A model that trains on GPUs does so through Ray, on at most 10 GPUs at
once.
