mkdir -p /etc/munge/
cp /etc/secrets/munge/munge.key /etc/munge/
chown munge:munge /etc/munge/munge.key
chmod 400 /etc/munge/munge.key
md5sum /etc/munge/munge.key >/etc/jupyter/test.txt
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

export PIXI_CACHE_DIR="/work/users/${NB_USER}/.pixi-cache/"

mv /etc/slurm/slist /usr/bin

cp /cvmfs/cms.cern.ch/SITECONF/T2_US_Purdue/storage.json /etc/cvmfs/ || true

bashrc_af_file=$NEW_HOME/.bashrc_af
touch $bashrc_af_file

bashrc_af_text='''
#!/bin/bash

# Ensure PATH includes system paths and pixi environment
# Prepend pixi paths, ensure system paths are always included at the end
SYSTEM_PATHS="/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin"
if [ -z "${PATH}" ]; then
    export PATH="/usr/local/bin:/opt/pixi/.pixi/envs/base-env/bin:/opt/pixi/bin:${SYSTEM_PATHS}"
else
    export PATH="/usr/local/bin:/opt/pixi/.pixi/envs/base-env/bin:/opt/pixi/bin:${PATH}:${SYSTEM_PATHS}"
fi

echo "
╔══════════════════════════════════════════════════════════════════════════════╗
║                                                                              ║
║     Purdue AF is migrating from Conda/Mamba to Pixi, as it is much           ║
║     faster and addresses multiple issues we have had with Conda.             ║
║     See pixi.sh for Pixi documentation.                                      ║
║                                                                              ║
║     To activate a Pixi environment (the project must NOT be in /home/):      ║
║         cd /path/to/project/containing/pixi.toml                             ║
║         pixi shell                                                           ║
║                                                                              ║
║     To deactivate a Pixi environment:                                        ║
║         exit                                                                 ║
║                                                                              ║
║     Conda commands are still available for backward compatibility.           ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
"

alias eos-connect="source /etc/jupyter/eos-connect.sh"
'''
echo "$bashrc_af_text" >$bashrc_af_file

bashrc_file=$NEW_HOME/.bashrc
touch $bashrc_file

# Initialize conda in bashrc_af
CONDA_BASE="/opt/pixi/.pixi/envs/base-env"
if [ -f "$CONDA_BASE/bin/conda" ]; then
	cat >>"$bashrc_af_file" <<'EOF'

# >>> conda initialize >>>
__conda_setup="$('/opt/pixi/.pixi/envs/base-env/bin/conda' 'shell.bash' 'hook' 2> /dev/null)"
if [ $? -eq 0 ]; then
    eval "$__conda_setup"
else
    if [ -f "/opt/pixi/.pixi/envs/base-env/etc/profile.d/conda.sh" ]; then
        . "/opt/pixi/.pixi/envs/base-env/etc/profile.d/conda.sh"
    else
        export PATH="/opt/pixi/.pixi/envs/base-env/bin:$PATH"
    fi
fi
unset __conda_setup
# <<< conda initialize <<<
[ -n "${CONDA_DEFAULT_ENV:-}" ] && conda deactivate 2>/dev/null || true
EOF
fi

extra_bashrc="source /home/$NB_USER/.bashrc_af"
grep -qxF "$extra_bashrc" "$bashrc_file" || echo "$extra_bashrc" >>"$bashrc_file"

echo """
bash
source $bashrc_file
""" >$NEW_HOME/.profile

cp .bashrc .bash_profile
