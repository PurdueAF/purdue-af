GPUs at Purdue AF 
============================

**Graphics processing units (GPUs)** are specialized processors that can
dramatically accelerate execution of parallelizable algorithms.

The most common use cases for GPUs in high energy physics are
**training** and **inference** of machine learning models,
however there are other frameworks and algorithms optimized to run on GPUs.

How to access GPUs at Purdue AF
--------------------------------------------------

1. **Direct connection**

   At Purdue AF, you can start a session with an interactive access to an
   **Nvidia A100** GPU. To achieve that, select a GPU when creating a session
   (see screenshot below).
   You will have a choice of either a 5GB "slice" of A100, or a full 40GB A100.

  .. image:: images/gpu-selection.png
    :width: 400
    :align: center

  .. note::

     If you selected a GPU, your session will have ``CUDA 12.2`` and
     ``cudnn 8.9.7.29``libraries loaded. Take this into account if you need
     to install particular versions of ML libraries such as ``tensorflow``
     - these libraries are notoriously sensitive to CUDA version.

  .. important::

     Please terminate your session after using a GPU in order to release the GPU
     for other users.

2. **Submit Slurm jobs (Purdue users only)**

   You can use Slurm to submit multiple GPU jobs to run in parallel. To request
   a GPU for a Slurm job, simply add ``--gpus-per-node=1`` argument to ``sbatch``
   command.

   - If you submit Slurm jobs directly from the Purdue AF inteface, they will be
     submitted to the Hammer cluster, which currectly features 13 nodes with
     **Nvidia T4** GPUs.
    
   - If you need more GPUs, or different GPU models, you may consider submitting
     Slurm jobs at Gilbreth cluster. To log in to Gilbreth cluster directly from
     the Purdue AF interface, simply run command ``ssh gilbreth`` and use BoilerKey
     credentials. Once you have logged in, you can use Slurm queues on Gilbreth
     cluster to run GPU jobs.

     .. important::

        The `only` storage volume shared between Purdue AF and the Gilbreth cluster
        is ``/depot/``; consider saving the outputs of your jobs there.

   
How to enable GPU support in common ML libraries
--------------------------------------------------

- ``Tensorflow``:
- ``Pytorch``:
- ``ONNX``: