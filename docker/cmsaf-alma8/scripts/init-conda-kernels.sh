# echo "Running conda kernel setup as $NB_USER"

eval "$(command conda shell.bash hook 2> /dev/null)"
conda init bash
export CONDARC=/home/$NB_USER/.condarc
echo CONDARC=$CONDARC

python3 /etc/jupyter/init-conda-kernels.py

# eval "$(command conda shell.bash hook 2> /dev/null)"
# export CONDARC=/home/$NB_USER/.condarc
# echo CONDARC=$CONDARC

# # user-installed kernels from conda environments
# conda env list
# for env in $(conda env list | tail -n +3 | rev | cut -d" " -f1 | rev); do 
#     if [[ $env != *"#"* && $env != "/opt/conda" ]]; then
#         conda activate $env;
#         envname=$(echo $env | rev | cut -d "/" -f1 | rev)
#         envname=$(echo "$envname" | tr '[:upper:]' '[:lower:]')
#         echo $envname
#         ipython kernel install --prefix=/opt/conda --name=$envname #&> /dev/null
#         kernel_path="/opt/conda/share/jupyter/kernels/"

#         #ipython kernel install --prefix=/home/$NB_USER/.local/ --name=$envname 
#         #kernel_path="/home/$NB_USER/.local/share/jupyter/kernels/"

#         #ipython kernel install --name=$envname #&> /dev/null
#         #kernel_path="/usr/local/share/jupyter/kernels/"

#         wrapper=$kernel_path$envname/wrapper.sh
#         echo $'#!/bin/bash\neval "$(command conda shell.bash hook 2> /dev/null)"\nconda activate ' \
#         $env $'\nexec' $env'/bin/python "$@"' > $wrapper
#         chmod 777 $kernel_path$envname/*
#         sed -i "3s#.*#  \"$wrapper\",#" $kernel_path$envname/kernel.json
#         #sed -i "s#$old#$wrapper#g" $kernel_path$envname/kernel.json
#         conda deactivate
#     fi
# done

# # pre-installed kernels
# conda activate /depot/cms/kernels/python3;
# ipython kernel install --prefix /opt/conda/ --name=python3 --display-name "Pyhton3 kernel (default)";
# conda deactivate;
# conda activate /depot/cms/kernels/python3-ml;
# ipython kernel install --prefix /opt/conda/ --name=python3-ml --display-name "Python3 kernel [ML]";
# conda deactivate;

# conda deactivate
# jupyter kernelspec list