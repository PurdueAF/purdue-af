mkdir -p /etc/munge/
cp /etc/secrets/munge/munge.key /etc/munge/
chown munge:munge /etc/munge/munge.key
chmod 400 /etc/munge/munge.key
md5sum /etc/munge/munge.key > /etc/jupyter/test.txt
sudo su -l munge -s /usr/sbin/munged

rm -rf $(pwd)/.jupyter/migrated
touch $(pwd)/.jupyter/migrated
chmod 777 $(pwd)/.jupyter/migrated
mkdir -p $(pwd)/.jupyter/lab/workspaces
mkdir -p $(pwd)/.local
chown -R $NB_USER:users ~/.[^.]*