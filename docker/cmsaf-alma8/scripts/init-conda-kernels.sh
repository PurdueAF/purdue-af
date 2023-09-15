eval "$(command conda shell.bash hook 2> /dev/null)"
conda init bash
export CONDARC=/home/$NB_USER/.condarc
echo CONDARC=$CONDARC

jupyter kernelspec remove -y python3

conda run -p /depot/cms/kernels/python3 ipython kernel install \
    --prefix=/opt/conda --name="python3"  --display-name "Python3 kernel (default)"
conda run -p /depot/cms/kernels/python3-ml ipython kernel install \
    --prefix=/opt/conda --name="python3-ml"  --display-name "Python3 kernel [ML]"

jupyter kernelspec list
