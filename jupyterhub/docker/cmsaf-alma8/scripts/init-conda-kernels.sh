eval "$(command conda shell.bash hook 2> /dev/null)"
conda init bash
export CONDARC=/home/$NB_USER/.condarc
echo CONDARC=$CONDARC

jupyter kernelspec remove -y python3

conda run -p /depot/cms/kernels/python3 ipython kernel install \
    --prefix=/opt/conda --name="python3"  --display-name "Python3 kernel (default)"

kernel_path="/opt/conda/share/jupyter/kernels/python3/"
LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/depot/cms/purdue-af/lhapdf/lib
PYTHONPATH=$PYTHONPATH:/depot/cms/purdue-af/lhapdf/lib/python3.10/site-packages
PATH=$PATH:/depot/cms/purdue-af/lhapdf/bin

LD_LIBRARY_PATH="$LD_LIBRARY_PATH" \
PYTHONPATH="$PYTHONPATH" \
PATH="$PATH" \
    jq '.env = {"PATH": env.PATH, "PYTHONPATH": env.PYTHONPATH, "LD_LIBRARY_PATH": env.LD_LIBRARY_PATH}' \
    "$kernel_path/kernel.json" > tmp_kernel.json
mv tmp_kernel.json "$kernel_path/kernel.json"

# For HATS 2024 workshop
# conda run -p /depot/cms/kernels/hats2024 ipython kernel install \
#     --prefix=/opt/conda --name="hats2024"  --display-name "HATS 2024"

# For Coffea_latest
conda run -p /depot/cms/kernels/coffea_latest ipython kernel install \
    --prefix=/opt/conda --name="coffea_latest"  --display-name "coffea_latest"

# kernel_path="/opt/conda/share/jupyter/kernels/hats2024/"
# "$kernel_path/kernel.json" > tmp_kernel.json

# ls -ltr $kernel_path

# mv tmp_kernel.json "$kernel_path/kernel.json"

# ------------------


jupyter kernelspec list
