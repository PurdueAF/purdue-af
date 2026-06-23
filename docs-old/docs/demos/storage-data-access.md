# Accessing files in different storage locations

In this demo, we demonstrate how data can be accessed from different storage spaces available to a Purdue Analysis Facility user. Copies of the same ROOT file have been placed in different storage locations, in this example they are accessed via `uproot.open()`.


```python
import uproot
import time

def test_file_access(path, filename):
    '''
    This function opens a NanoAOD file at a given path
    and prints the pT of the first muon found in that file,
    as well as the time taken to access the file.
    '''

    print(f"Accessing file at {path}{filename}")
    start = time.time()
    with uproot.open(path+filename)["Events"] as file:
        pt = file["Muon_pt"].array()[0][0]
        print("First muon pT: ", round(pt, 2), "GeV")

    dt = round(time.time()-start, 2)
    print(f"Time elapsed: {dt}s.")
    print()

```

### 0. Local files


```python
test_file_access("./data/", "test_nanoaod.root")
```

    Accessing file at ./data/test_nanoaod.root
    First muon pT:  82.55 GeV
    Time elapsed: 3.91s.
    


### 1. Purdue Depot
The Purdue Depot storage is writeable for Purdue users, but read-only for external users.


```python
# Choice of two paths - the mount point and the symlink in home directory
# (the latter is convenient for access via the file browser).

filename = "test_nanoaod_depot.root"
test_file_access("/depot/cms/hmm/", filename)
test_file_access("~/depot/hmm/", filename)
```

    Accessing file at /depot/cms/hmm/test_nanoaod_depot.root
    First muon pT:  82.55 GeV
    Time elapsed: 0.87s.
    
    Accessing file at ~/depot/hmm/test_nanoaod_depot.root
    First muon pT:  82.55 GeV
    Time elapsed: 0.71s.
    


### 2. Purdue EOS
Purdue EOS storage is mounted as read-only FS; however, Purdue users can write to their EOS directories using `gfal` commands [(see instrictions here)](https://www.physics.purdue.edu/Tier2/user-info/tutorials/dfs_commands.php).


```python
# Choice of two paths - the mount point and the symlink in home directory
# (the latter is convenient for access via the file browser).

filename = "test_nanoaod_eos_purdue.root"
test_file_access("/eos/purdue/store/user/dkondrat/", filename)
test_file_access("~/eos-purdue/store/user/dkondrat/", filename)
```

    Accessing file at /eos/purdue/store/user/dkondrat/test_nanoaod_eos_purdue.root
    First muon pT:  82.55 GeV
    Time elapsed: 0.82s.
    
    Accessing file at ~/eos-purdue/store/user/dkondrat/test_nanoaod_eos_purdue.root
    First muon pT:  82.55 GeV
    Time elapsed: 0.79s.
    


### 3. CERN EOS (CERNBox)

<div class="alert alert-warning">
Warning

The following cell must be modified, since each user only gets access to their own CERNBox directory.
To enable access to your CERNBox, run command `eos-connect` in terminal.
</div>


```python
# Choice of two paths - the mount point and the symlink in home directory
# (the latter is convenient for access via the file browser).

filename = "test_nanoaod_eos_cern.root"
test_file_access("/eos/cern/home-d/dkondrat/", filename)
test_file_access("~/eos-cern/", filename)
```

    Accessing file at /eos/cern/home-d/dkondrat/test_nanoaod_eos_cern.root
    First muon pT:  82.55 GeV
    Time elapsed: 5.31s.
    
    Accessing file at ~/eos-cern/test_nanoaod_eos_cern.root
    First muon pT:  82.55 GeV
    Time elapsed: 0.77s.
    


### 4. XRootD
Before using XRootD, initialize the VOMS proxy in terminal (`voms-proxy-init --voms cms`)


```python
test_file_access("root://eos.cms.rcac.purdue.edu//store/user/dkondrat/", "test_nanoaod_eos_purdue.root")
```

    Accessing file at root://eos.cms.rcac.purdue.edu//store/user/dkondrat/test_nanoaod_eos_purdue.root
    First muon pT:  82.55 GeV
    Time elapsed: 2.03s.
    


### 5. CVMFS


```python
!ls /cvmfs/cms.cern.ch/cmsset_default.sh
```

    /cvmfs/cms.cern.ch/cmsset_default.sh



```python

```
