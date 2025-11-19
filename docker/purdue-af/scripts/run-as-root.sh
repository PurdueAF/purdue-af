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

echo "
# Purdue AF is gradually migrading from Conda/Mamba to Pixi.
#
# To activate a Pixi environment:
#     cd /path/to/project/containing/pixi.toml
#     pixi shell
#
# To deactivate a Pixi environment:
#     exit
"

# DEBUG: Conda initialization status
echo ""
echo "=== DEBUG: Conda Status ==="
if command -v conda >/dev/null 2>&1; then
    echo "✓ conda command found: $(which conda)"
    if conda --version >/dev/null 2>&1; then
        echo "✓ conda version: $(conda --version 2>&1)"
    else
        echo "✗ conda --version failed"
    fi
    # Check if conda is initialized in this shell
    if [ -n "${CONDA_DEFAULT_ENV:-}" ] || [ -n "${CONDA_PREFIX:-}" ]; then
        echo "✓ conda is active (env: ${CONDA_DEFAULT_ENV:-none}, prefix: ${CONDA_PREFIX:-none})"
    else
        echo "⚠ conda command available but not activated in this shell"
    fi
    # Check if conda init block exists in bashrc
    if grep -q "# >>> conda initialize >>>" "$HOME/.bashrc" 2>/dev/null; then
        echo "✓ conda init block found in ~/.bashrc"
    else
        echo "✗ conda init block NOT found in ~/.bashrc"
    fi
else
    echo "✗ conda command NOT found in PATH"
    echo "  PATH: ${PATH}"
    echo "  Checking common locations:"
    [ -f "/opt/pixi/.pixi/envs/base-env/bin/conda" ] && echo "    ✓ /opt/pixi/.pixi/envs/base-env/bin/conda exists" || echo "    ✗ /opt/pixi/.pixi/envs/base-env/bin/conda NOT found"
fi
echo "=== End Conda Status ==="
echo ""

alias eos-connect="source /etc/jupyter/eos-connect.sh"
'''
echo "$bashrc_af_text" >$bashrc_af_file

bashrc_file=$NEW_HOME/.bashrc
touch $bashrc_file

# Initialize conda if available (installed via pixi in base-env)
# PATH is already set in Dockerfile ENV, so conda should be available
if command -v conda >/dev/null 2>&1; then
	# Run conda init bash and append to bashrc if not already initialized
	if ! grep -q "# >>> conda initialize >>>" "$bashrc_file"; then
		conda init bash >>"$bashrc_file" 2>&1 || true
	fi
fi

extra_bashrc="source /home/$NB_USER/.bashrc_af"
grep -qxF "$extra_bashrc" "$bashrc_file" || echo "$extra_bashrc" >>"$bashrc_file"

echo """
bash
source $bashrc_file
""" >$NEW_HOME/.profile

cp .bashrc .bash_profile
