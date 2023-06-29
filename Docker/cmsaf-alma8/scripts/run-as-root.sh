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

cp /etc/jupyter/dask/jobqueue-purdue-slurm.yaml $NEW_HOME/.config/dask/
chown -R $NB_USER:users $NEW_HOME/.config/dask/*