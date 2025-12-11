#!/bin/bash
# Profiling: Enabled by default. Set PROFILE=0 to disable.
# This tracks execution time for each line to identify performance bottlenecks.
export PROFILE="${PROFILE:-1}"

if [ "${PROFILE:-0}" = "1" ]; then
	PROFILE_LOG="${PROFILE_LOG:-/tmp/run-as-root-profile.log}"
	SCRIPT_START=$(date +%s.%N 2>/dev/null || date +%s)

	# Simple approach: use set -x with timestamped PS4
	# Output will show each command with line number and timestamp
	PS4='+ [$(date +%s.%N 2>/dev/null || date +%s)] Line $LINENO: '
	exec 3>&2 2> >(tee "$PROFILE_LOG" >&3 | sed -u 's/^+/[PROFILE] +/')
	set -x
fi

mkdir -p /etc/munge/
cp /etc/secrets/munge/munge.key /etc/munge/
chown munge:munge /etc/munge/munge.key
chmod 400 /etc/munge/munge.key
md5sum /etc/munge/munge.key >/etc/jupyter/test.txt
su -l munge -s /usr/sbin/munged

NEW_HOME=/home/$NB_USER
mkdir -p $NEW_HOME/.jupyter
rm -rf $NEW_HOME/.jupyter/migrated
touch $NEW_HOME/.jupyter/migrated
chmod 777 $NEW_HOME/.jupyter/migrated
mkdir -p $NEW_HOME/.jupyter/lab/workspaces
mkdir -p $NEW_HOME/.local/share
mkdir -p $NEW_HOME/.config/dask
chown -R $NB_USER:users $NEW_HOME/.[^.]*

mkdir -p /work/users/$NB_USER
chmod 755 /work/users/$NB_USER
chown $NB_UID:users /work/users/$NB_USER

# Update pixi-kernel-python3 display name
BASE_ENV_DIR="/opt/pixi/.pixi/envs/base-env"
KERNEL_JSON="${BASE_ENV_DIR}/share/jupyter/kernels/pixi-kernel-python3/kernel.json"
if [ -f "${KERNEL_JSON}" ]; then
	if command -v jq >/dev/null 2>&1; then
		jq '.display_name = "Python (pixi project-aware)"' "${KERNEL_JSON}" >"${KERNEL_JSON}.tmp" &&
			mv "${KERNEL_JSON}.tmp" "${KERNEL_JSON}"
	else
		sed -i 's/"display_name": "[^"]*"/"display_name": "Python (pixi project-aware)"/' "${KERNEL_JSON}"
	fi
fi

# Install python3 kernel from pixi global env
PIXI_GLOBAL="/work/pixi"
if [ -d "${PIXI_GLOBAL}" ] && [ -f "${PIXI_GLOBAL}/pixi.toml" ]; then
	ORIGINAL_DIR=$(pwd)
	cd "${PIXI_GLOBAL}" || exit 1
	# Remove existing python3 kernel if it exists
	jupyter kernelspec remove -y python3 2>/dev/null || true
	# Install new kernel using pixi
	pixi run python -m ipykernel install --name python3 --display-name "Python (pixi global)" --prefix "${BASE_ENV_DIR}"
	cd "${ORIGINAL_DIR}" || exit 1
fi

export PIXI_CACHE_DIR="/work/users/${NB_USER}/.pixi-cache/"

mv /etc/slurm/slist /usr/bin

cp /cvmfs/cms.cern.ch/SITECONF/T2_US_Purdue/storage.json /etc/cvmfs/ || true

bashrc_af_file=$NEW_HOME/.bashrc_af && touch $bashrc_af_file

cat >"$bashrc_af_file" <<'EOF'
#!/bin/bash

# Ensure PATH includes system paths and pixi environment
# Prepend pixi paths, ensure system paths are always included at the end
SYSTEM_PATHS="/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin"
if [ -z "${PATH}" ]; then
    export PATH="/usr/local/bin:/opt/pixi/.pixi/envs/base-env/bin:/opt/pixi/bin:${SYSTEM_PATHS}"
else
    export PATH="/usr/local/bin:/opt/pixi/.pixi/envs/base-env/bin:/opt/pixi/bin:${PATH}:${SYSTEM_PATHS}"
fi

export NB_USER="${NB_USER}"
export NB_UID="${NB_UID}"
export NB_GID="${NB_GID}"
export PIXI_HOME="/opt/pixi"
export PIXI_CACHE_DIR="/work/users/${NB_USER}/.pixi-cache/"
export PYROSCOPE_SERVER="http://pyroscope.cms.svc.cluster.local:4040"
export PYROSCOPE_APP="purdue-af"
export DASK_GATEWAY__ADDRESS="http://dask-gateway-k8s.geddes.rcac.purdue.edu"
export DASK_GATEWAY__PROXY_ADDRESS="api-dask-gateway-k8s.cms.geddes.rcac.purdue.edu:8000"
export DASK_LABEXTENSION__FACTORY__MODULE="dask_gateway"
export DASK_LABEXTENSION__FACTORY__CLASS="GatewayCluster"
export DASK_LABEXTENSION__FACTORY__KWARGS__ADDRESS="http://dask-gateway-k8s.geddes.rcac.purdue.edu"
export DASK_LABEXTENSION__FACTORY__KWARGS__PROXY_ADDRESS="api-dask-gateway-k8s.cms.geddes.rcac.purdue.edu:8000"
export DASK_LABEXTENSION__FACTORY__KWARGS__PUBLIC_ADDRESS="https://dask-gateway-k8s.geddes.rcac.purdue.edu"
export X509_CERT_DIR="/cvmfs/cms.cern.ch/grid/etc/grid-security/certificates"

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
EOF

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

# Make .bashrc_af read-only for user (system-managed file)
chown root:root $bashrc_af_file
chmod 644 $bashrc_af_file

echo """
bash
source $bashrc_file
""" >$NEW_HOME/.profile

cp .bashrc .bash_profile

# Finalize profiling if enabled
if [ "${PROFILE:-0}" = "1" ]; then
	set +x
	exec 2>&3 3>&-
	SCRIPT_END=$(date +%s.%N 2>/dev/null || date +%s)
	TOTAL_TIME=$(awk "BEGIN {printf \"%.6f\", $SCRIPT_END - $SCRIPT_START}" 2>/dev/null || echo "0")
	echo "" >&2
	echo "[PROFILE] ========================================" >&2
	echo "[PROFILE] Total execution time: ${TOTAL_TIME}s" >&2
	echo "[PROFILE] Detailed log with timestamps: $PROFILE_LOG" >&2
	echo "[PROFILE] Each line shows: [timestamp] Line N: command" >&2
	echo "[PROFILE] Calculate elapsed time by subtracting consecutive timestamps" >&2
	echo "[PROFILE] ========================================" >&2
fi
