#!/bin/bash

# Configuration
NEW_HOME="/home/$NB_USER"
BASE_ENV_DIR="/opt/pixi/.pixi/envs/base-env"
PIXI_GLOBAL="/work/pixi/global"
PIXI_GLOBAL_PYTHON="${PIXI_GLOBAL}/.pixi/envs/default/bin/python"
export PIXI_CACHE_DIR="/work/users/${NB_USER}/.pixi-cache/"

# Setup munge authentication
mkdir -p /etc/munge/
cp /etc/secrets/munge/munge.key /etc/munge/
chown munge:munge /etc/munge/munge.key
chmod 400 /etc/munge/munge.key
md5sum /etc/munge/munge.key >/etc/jupyter/test.txt
su -l munge -s /usr/sbin/munged

# Setup user home directory
mkdir -p "$NEW_HOME/.jupyter"
rm -rf "$NEW_HOME/.jupyter/migrated"
touch "$NEW_HOME/.jupyter/migrated"
chmod 777 "$NEW_HOME/.jupyter/migrated"
mkdir -p "$NEW_HOME/.jupyter/lab/workspaces"
mkdir -p "$NEW_HOME/.local/share"
mkdir -p "$NEW_HOME/.config/dask"
chown -R $NB_USER:users $NEW_HOME/.[^.]*
chmod 755 "$NEW_HOME"
# .ssh: directory 700 (required by SSH); key/authorized_keys files 600 (not 700)
if [ -d "$NEW_HOME/.ssh" ]; then
	chmod 700 "$NEW_HOME/.ssh"
	chmod 600 "$NEW_HOME/.ssh"/* 2>/dev/null || true
fi

# Setup work directory
mkdir -p "/work/users/$NB_USER"
chmod 755 "/work/users/$NB_USER"
chown "$NB_UID:users" "/work/users/$NB_USER"

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

	# 1) Install and patch in base env (single source of truth)
	jupyter kernelspec remove -y python3 2>/dev/null || true
	"${PIXI_GLOBAL_PYTHON}" -m ipykernel install --name python3 --display-name "Python (pixi global)" --prefix "${BASE_ENV_DIR}"
	BASE_PY3_KERNEL_JSON="${BASE_ENV_DIR}/share/jupyter/kernels/python3/kernel.json"
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

	# 2) Mirror same kernel to user space (~/.local/share/jupyter) for VSCode-style discovery
	BASE_PY3_KERNEL="${BASE_ENV_DIR}/share/jupyter/kernels/python3"
	USER_KERNEL_DIR="${NEW_HOME}/.local/share/jupyter/kernels"
	if [ -d "${BASE_PY3_KERNEL}" ]; then
		mkdir -p "${USER_KERNEL_DIR}"
		rm -rf "${USER_KERNEL_DIR}/python3"
		cp -r "${BASE_PY3_KERNEL}" "${USER_KERNEL_DIR}/python3"
		chown -R "${NB_USER}:users" "${USER_KERNEL_DIR}"
	fi
fi

# Fix DNS resolution for pixi: IPv6 is enabled but unreachable in Kubernetes
# DNS returns IPv6 addresses first, causing pixi's Rust DNS resolver to fail
# Solution: Dynamically resolve and add IPv4 addresses to /etc/hosts for pixi-related domains
# (Kubernetes overwrites /etc/hosts, so we must do this at runtime)
if ! grep -q "# Fix for pixi DNS resolution" /etc/hosts 2>/dev/null; then
	# List of domains used by pixi (conda channels, PyPI, prefix.dev, GitHub)
	PIXI_DOMAINS=(
		"conda.anaconda.org"
		"anaconda.org"
		"repo.anaconda.com"
		"repo.continuum.io"
		"prefix.dev"
		"conda-mapping.prefix.dev"
		"pypi.org"
		"pypi.python.org"
		"files.pythonhosted.org"
		"github.com"
		"raw.githubusercontent.com"
		"api.github.com"
	)

	echo "" >>/etc/hosts
	echo "# Fix for pixi DNS resolution: IPv6 connectivity broken in K8s cluster" >>/etc/hosts
	echo "# Dynamically resolved IPv4 addresses for pixi-related domains" >>/etc/hosts

	# Resolve each domain to IPv4 and add to /etc/hosts
	for domain in "${PIXI_DOMAINS[@]}"; do
		# Use getent to get IPv4 address (ahostsv4 returns IPv4 only)
		ipv4=$(getent ahostsv4 "$domain" 2>/dev/null | head -1 | awk '{print $1}')
		if [ -n "$ipv4" ]; then
			echo "$ipv4 $domain" >>/etc/hosts
		fi
	done
fi

# Setup system files
mv /etc/slurm/slist /usr/bin
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
export PIXI_HOME="/opt/pixi"
export PIXI_CACHE_DIR="/work/users/${NB_USER}/.pixi-cache/"
export PYROSCOPE_SERVER="http://pyroscope.cms.svc.cluster.local:4040"
export PYROSCOPE_APP="purdue-af"
export DASK_GATEWAY__ADDRESS="http://dask-gateway-k8s.geddes.rcac.purdue.edu"
export DASK_GATEWAY__PROXY_ADDRESS="traefik-dask-gateway-k8s.cms.geddes.rcac.purdue.edu:8786"
export DASK_LABEXTENSION__FACTORY__MODULE="dask_gateway"
export DASK_LABEXTENSION__FACTORY__CLASS="GatewayCluster"
export DASK_LABEXTENSION__FACTORY__KWARGS__ADDRESS="http://dask-gateway-k8s.geddes.rcac.purdue.edu"
export DASK_LABEXTENSION__FACTORY__KWARGS__PROXY_ADDRESS="traefik-dask-gateway-k8s.cms.geddes.rcac.purdue.edu:8786"
export DASK_LABEXTENSION__FACTORY__KWARGS__PUBLIC_ADDRESS="https://dask-gateway-k8s.geddes.rcac.purdue.edu"
export X509_CERT_DIR="/cvmfs/cms.cern.ch/grid/etc/grid-security/certificates"

echo "
╔═════════════════════════════════════════════════════════════════════════════════════════╗
║                                                                                         ║
║     Purdue AF is migrating from Conda/Mamba to Pixi, as it is much faster and           ║
║     addresses multiple issues we have had with Conda.                                   ║
║                                                                                         ║
║     See instructions for migrating from Conda to Pixi:                                  ║
║         https://analysis-facility.physics.purdue.edu/en/latest/guide-conda-to-pixi.html ║
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
