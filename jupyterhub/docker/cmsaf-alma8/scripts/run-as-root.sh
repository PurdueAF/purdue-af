mkdir -p /etc/munge/
cp /etc/secrets/munge/munge.key /etc/munge/
chown munge:munge /etc/munge/munge.key
chmod 400 /etc/munge/munge.key
md5sum /etc/munge/munge.key > /etc/jupyter/test.txt
sudo su -l munge -s /usr/sbin/munged

NEW_HOME=/home/$NB_USER
rm -rf $NEW_HOME/.jupyter/migrated
touch $NEW_HOME/.jupyter/migrated
chmod 777 $NEW_HOME/.jupyter/migrated
mkdir -p $NEW_HOME/.jupyter/lab/workspaces
mkdir -p $NEW_HOME/.local
mkdir -p $NEW_HOME/.local/share
mkdir -p $NEW_HOME/.config/dask
chown -R $NB_USER:users $NEW_HOME/.[^.]*

mkdir -p /work/users/$NB_USER
chmod 755 /work/users/$NB_USER
chown $NB_UID:users /work/users/$NB_USER

# cp /etc/jupyter/dask/*.yaml $NEW_HOME/.config/dask/
# rm -f $NEW_HOME/.config/dask/gateway.yaml
# mkdir -p $NEW_HOME/.config/dask/
# chown -R $NB_USER:users $NEW_HOME/.config/dask/*

mv /etc/slurm/slist /usr/bin
export PATH=/etc/jupyter/dask/:$PATH

cp /cvmfs/cms.cern.ch/SITECONF/T2_US_Purdue/storage.json /etc/cvmfs/ || true

bashrc_af_file=$NEW_HOME/.bashrc_af
touch $bashrc_af_file

bashrc_af_text='''
#!/bin/bash

eval "$(command conda shell.bash hook 2> /dev/null)"
conda deactivate

echo "
# To activate the \"default\" conda environment in this Terminal, run the following command:
#
#     conda activate /depot/cms/kernels/python3
#
# This environment corresponds to \"Python3 kernel (default)\" in Jupyter notebooks.
"

alias eos-connect="source /etc/jupyter/eos-connect.sh"
resize
'''
echo "$bashrc_af_text" > $bashrc_af_file;

bashrc_file=$NEW_HOME/.bashrc
touch $bashrc_file

extra_bashrc="source /home/$NB_USER/.bashrc_af"
grep -qxF "$extra_bashrc" "$bashrc_file" || echo "$extra_bashrc" >> "$bashrc_file"

echo """
bash
source $bashrc_file
""" > $NEW_HOME/.profile

cp .bashrc .bash_profile