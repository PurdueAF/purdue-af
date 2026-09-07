#!/bin/bash

# Configuration
NEW_HOME="/home/$NB_USER"

# Anything this script writes *below* $NEW_HOME is written as the user: parts
# of a home can be symlinked onto /depot, where root_squash makes root the one
# identity that cannot write. Files directly in $NEW_HOME stay as they are —
# they are always on the CephFS home volume, and .bashrc_af is deliberately
# root-owned.
source /usr/local/bin/af-as-user.sh
BASE_ENV_DIR="/opt/pixi/.pixi/envs/base-env"
PIXI_GLOBAL="/work/pixi/global"
PIXI_GLOBAL_PYTHON="${PIXI_GLOBAL}/.pixi/envs/default/bin/python"
export PIXI_CACHE_DIR="/work/users/${NB_USER}/.pixi-cache/"

# Setup munge authentication
if [ -f /etc/secrets/munge/munge.key ]; then
	mkdir -p /etc/munge/
	install -m 400 -o munge -g munge /etc/secrets/munge/munge.key /etc/munge/munge.key
	# kubectl cp often leaves the PVC file world-readable; tighten if the mount is writable
	chmod 400 /etc/secrets/munge/munge.key 2>/dev/null || true
	chown root:munge /etc/secrets/munge/munge.key 2>/dev/null || true
	su -l munge -s /usr/sbin/munged
fi

# Setup user home directory
# Gated by a versioned sentinel so chown/mkdir only run once per user.
# Bump _HOME_SETUP_VER whenever new directories are added here.
_HOME_SETUP_VER=2
_HOME_SETUP_SENTINEL="$NEW_HOME/.jupyter/.af-home-setup-v${_HOME_SETUP_VER}"
if [ ! -f "$_HOME_SETUP_SENTINEL" ]; then
	# Best effort: a home the user cannot fully write (a dangling symlink, a
	# full quota) costs them these directories, never the whole session.
	af_as_user mkdir -p "$NEW_HOME/.jupyter/lab/workspaces" || true
	af_as_user mkdir -p "$NEW_HOME/.local/share/jupyter/runtime" || true
	af_as_user mkdir -p "$NEW_HOME/.config/dask" || true
	# Created as the user above, so this only repairs homes an older image left
	# root-owned — and it cannot work through a depot symlink, so never fatal.
	chown -h "$NB_USER:users" \
		"$NEW_HOME/.jupyter" \
		"$NEW_HOME/.jupyter/lab" \
		"$NEW_HOME/.jupyter/lab/workspaces" \
		"$NEW_HOME/.local" \
		"$NEW_HOME/.local/share" \
		"$NEW_HOME/.local/share/jupyter" \
		"$NEW_HOME/.local/share/jupyter/runtime" \
		"$NEW_HOME/.config" \
		"$NEW_HOME/.config/dask" || true
	af_as_user touch "$_HOME_SETUP_SENTINEL" || true
fi
chmod 755 "$NEW_HOME"
# Recreate the migrated flag every start — prevents the Jupyter migration dialog
# and is cheap (3 ops on a single small file).
af_as_user mkdir -p "$NEW_HOME/.jupyter" || true
af_as_user rm -rf "$NEW_HOME/.jupyter/migrated" || true
af_as_user touch "$NEW_HOME/.jupyter/migrated" || true
af_as_user chmod 777 "$NEW_HOME/.jupyter/migrated" || true
# .ssh: directory 700 (required by SSH); key/authorized_keys files 600 (not 700)
if [ -d "$NEW_HOME/.ssh" ]; then
	af_as_user chmod 700 "$NEW_HOME/.ssh" || true
	af_as_user chmod 600 "$NEW_HOME/.ssh"/* 2>/dev/null || true
fi

# Setup work directory
mkdir -p "/work/users/$NB_USER"
chmod 755 "/work/users/$NB_USER"
chown "$NB_UID:users" "/work/users/$NB_USER"
# Writable Pixi user home (CLI global layout); image stack under /opt/pixi may be read-only.
mkdir -p "/work/users/$NB_USER/.pixi-home"
chown "$NB_UID:users" "/work/users/$NB_USER/.pixi-home"

# Update pixi-kernel-python3 display name
KERNEL_JSON="${BASE_ENV_DIR}/share/jupyter/kernels/pixi-kernel-python3/kernel.json"
if [ -f "${KERNEL_JSON}" ]; then
	if command -v jq >/dev/null 2>&1; then
		jq '.display_name = "Python (pixi project-aware)"' "${KERNEL_JSON}" >"${KERNEL_JSON}.tmp" &&
			mv "${KERNEL_JSON}.tmp" "${KERNEL_JSON}"
	else
		sed -i 's/"display_name": "[^"]*"/"display_name": "Python (pixi project-aware)"/' "${KERNEL_JSON}"
	fi
fi

# Install pixi-global kernel as python3 in both base env and user space (JupyterLab vs VSCode discovery)
if [ -d "${PIXI_GLOBAL}" ] && [ -f "${PIXI_GLOBAL}/pixi.toml" ] && [ -f "${PIXI_GLOBAL_PYTHON}" ]; then
	PIXI_GLOBAL_PREFIX="${PIXI_GLOBAL}/.pixi/envs/default"
	PIXI_GLOBAL_SYSROOT_INC="${PIXI_GLOBAL_PREFIX}/x86_64-conda-linux-gnu/sysroot/usr/include"
	PIXI_GLOBAL_BIN="${PIXI_GLOBAL_PREFIX}/bin"

	BASE_PY3_KERNEL_JSON="${BASE_ENV_DIR}/share/jupyter/kernels/python3/kernel.json"

	# 1) Install and patch in base env — skip if kernel already points to the correct Python
	_needs_install=true
	if [ -f "${BASE_PY3_KERNEL_JSON}" ] && command -v jq >/dev/null 2>&1; then
		_current_python=$(jq -r '.argv[0] // empty' "${BASE_PY3_KERNEL_JSON}" 2>/dev/null)
		if [ "${_current_python}" = "${PIXI_GLOBAL_BIN}/python" ]; then
			_needs_install=false
		fi
	fi

	if [ "${_needs_install}" = "true" ]; then
		jupyter kernelspec remove -y python3 2>/dev/null || true
		export JUPYTER_PATH="${BASE_ENV_DIR}/share/jupyter${JUPYTER_PATH:+:${JUPYTER_PATH}}"
		"${PIXI_GLOBAL_PYTHON}" -m ipykernel install --name python3 --display-name "Python (pixi global)" --prefix "${BASE_ENV_DIR}"
		if [ -f "${BASE_PY3_KERNEL_JSON}" ] && command -v jq >/dev/null 2>&1; then
			jq \
				--arg python "${PIXI_GLOBAL_BIN}/python" \
				--arg inc "${PIXI_GLOBAL_SYSROOT_INC}" \
				--arg path_val "${PIXI_GLOBAL_BIN}:\${PATH}" \
				'.argv = [$python, "-m", "ipykernel_launcher", "-f", "{connection_file}"] | .display_name = "Python (pixi global)" | .language = "python" | .metadata = {"debugger": true} | .env = {"C_INCLUDE_PATH": $inc, "CPLUS_INCLUDE_PATH": $inc, "PATH": $path_val}' \
				"${BASE_PY3_KERNEL_JSON}" >"${BASE_PY3_KERNEL_JSON}.tmp" &&
				mv "${BASE_PY3_KERNEL_JSON}.tmp" "${BASE_PY3_KERNEL_JSON}"
		elif [ -f "${BASE_PY3_KERNEL_JSON}" ]; then
			echo "Warning: jq not found; cannot write pixi global kernel spec at ${BASE_PY3_KERNEL_JSON}." >&2
		fi
	fi

	# 2) Mirror same kernel to user space (~/.local/share/jupyter) for VSCode-style discovery
	BASE_PY3_KERNEL="${BASE_ENV_DIR}/share/jupyter/kernels/python3"
	USER_KERNEL_DIR="${NEW_HOME}/.local/share/jupyter/kernels"
	if [ -d "${BASE_PY3_KERNEL}" ]; then
		af_as_user mkdir -p "${USER_KERNEL_DIR}" || true
		af_as_user rm -rf "${USER_KERNEL_DIR}/python3" || true
		af_as_user cp -r "${BASE_PY3_KERNEL}" "${USER_KERNEL_DIR}/python3" || true
	fi
fi

# Everything under ~/.local is now created as the user, so this is only a repair
# for homes an older image left root-owned. It runs as root and therefore cannot
# touch a depot-backed ~/.local at all — which is exactly the case it must not
# abort on, since start.sh sources this file under `set -e`.
_JUPYTER_USER_DATA="$NEW_HOME/.local/share/jupyter"
af_as_user mkdir -p "$_JUPYTER_USER_DATA/runtime" || true
for _d in "$NEW_HOME/.local" "$NEW_HOME/.local/share" "$_JUPYTER_USER_DATA" "$_JUPYTER_USER_DATA/runtime"; do
	# -h, and skip symlinks for chmod: both follow links by default, and these
	# paths are user-controlled. Root dereferencing a symlink the user planted
	# is how a chown of ~/.local turns into a chown of something else entirely.
	[ -L "$_d" ] && continue
	chown -h "$NB_USER:users" "$_d" || true
	chmod u+rwx "$_d" || true
done

# Setup system files
if [ -f /etc/slurm/slist ]; then
	mv /etc/slurm/slist /usr/bin/slist
	chmod 755 /usr/bin/slist
fi
cp /cvmfs/cms.cern.ch/SITECONF/T2_US_Purdue/storage.json /etc/cvmfs/ || true

# Create bashrc_af file
bashrc_af_file="$NEW_HOME/.bashrc_af"
touch "$bashrc_af_file"

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
export PIXI_HOME="/work/users/${NB_USER}/.pixi-home"
export PIXI_CACHE_DIR="/work/users/${NB_USER}/.pixi-cache/"
export PYROSCOPE_SERVER="http://pyroscope.cms.svc.cluster.local:4040"
export PYROSCOPE_APP="purdue-af"
export DASK_GATEWAY__ADDRESS="http://dask-gateway-k8s.geddes.rcac.purdue.edu"
export DASK_GATEWAY__PROXY_ADDRESS="traefik-dask-gateway-k8s.cms.geddes.rcac.purdue.edu:8786"
export X509_CERT_DIR="/cvmfs/cms.cern.ch/grid/etc/grid-security/certificates"

echo "
╔═════════════════════════════════════════════════════════════════════════════════════════╗
║                                                                                         ║
║             Join Purdue AF support channel on CERN Mattermost:                          ║
║       https://mattermost.web.cern.ch/cms-exp/channels/purdue-analysis-facility          ║
║                                                                                         ║
╠═════════════════════════════════════════════════════════════════════════════════════════╣
║                                                                                         ║
║     To activate a Pixi environment (the project must NOT be in /home/):                 ║
║         cd /path/to/project/containing/pixi.toml                                        ║
║         pixi shell                                                                      ║
║                                                                                         ║
║     To deactivate a Pixi environment:                                                   ║
║         exit                                                                            ║
║                                                                                         ║
╚═════════════════════════════════════════════════════════════════════════════════════════╝
"

alias eos-connect="source /etc/jupyter/eos-connect.sh"
EOF

# Initialize conda in bashrc_af
if [ -f "${BASE_ENV_DIR}/bin/conda" ]; then
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

# Link bashrc_af into bashrc
bashrc_file="$NEW_HOME/.bashrc"
touch "$bashrc_file"
extra_bashrc="source $NEW_HOME/.bashrc_af"
grep -qxF "$extra_bashrc" "$bashrc_file" || echo "$extra_bashrc" >>"$bashrc_file"

# Make .bashrc_af read-only for user (system-managed file)
chown root:root "$bashrc_af_file"
chmod 644 "$bashrc_af_file"

# Create .profile
cat >"$NEW_HOME/.profile" <<EOF
bash
source $bashrc_file
EOF

# Create .bash_profile from .bashrc if it exists
[ -f "$bashrc_file" ] && cp "$bashrc_file" "$NEW_HOME/.bash_profile"
