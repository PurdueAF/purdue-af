#!/bin/bash

NEW_HOME="/home/$NB_USER"

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
# Best effort: a home the user cannot fully write (a dangling symlink, a full
# quota) costs them these directories, never the whole session.
af_as_user mkdir -p "$NEW_HOME/.jupyter/lab/workspaces" "$NEW_HOME/.local/share/jupyter/runtime" || true
# Repairs a root-owned home. User-controlled paths: never let chown/chmod follow
# a symlink, and a depot-backed path is out of root's reach, so never fatal.
for _d in "$NEW_HOME/.jupyter" "$NEW_HOME/.jupyter/lab" "$NEW_HOME/.jupyter/lab/workspaces" \
	"$NEW_HOME/.local" "$NEW_HOME/.local/share" "$NEW_HOME/.local/share/jupyter" \
	"$NEW_HOME/.local/share/jupyter/runtime" "$NEW_HOME/.config"; do
	[ -L "$_d" ] && continue
	chown -h "$NB_USER:users" "$_d" 2>/dev/null || true
	chmod u+rwx "$_d" 2>/dev/null || true
done
chmod 755 "$NEW_HOME"
# Its presence prevents the Jupyter migration dialog.
[ -e "$NEW_HOME/.jupyter/migrated" ] || af_as_user touch "$NEW_HOME/.jupyter/migrated" || true
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

# Install pixi-global kernel as python3 in both base env and user space (JupyterLab vs VSCode discovery)
if [ -d "${PIXI_GLOBAL}" ] && [ -f "${PIXI_GLOBAL}/pixi.toml" ] && [ -f "${PIXI_GLOBAL_PYTHON}" ]; then
	PIXI_GLOBAL_PREFIX="${PIXI_GLOBAL}/.pixi/envs/default"
	PIXI_GLOBAL_SYSROOT_INC="${PIXI_GLOBAL_PREFIX}/x86_64-conda-linux-gnu/sysroot/usr/include"
	PIXI_GLOBAL_BIN="${PIXI_GLOBAL_PREFIX}/bin"

	BASE_PY3_KERNEL_JSON="${BASE_ENV_DIR}/share/jupyter/kernels/python3/kernel.json"

	# 1) Install and patch in base env
	export JUPYTER_PATH="${BASE_ENV_DIR}/share/jupyter${JUPYTER_PATH:+:${JUPYTER_PATH}}"
	if "${PIXI_GLOBAL_PYTHON}" -m ipykernel install --name python3 --display-name "Python (pixi global)" --prefix "${BASE_ENV_DIR}"; then
		jq \
			--arg python "${PIXI_GLOBAL_BIN}/python" \
			--arg inc "${PIXI_GLOBAL_SYSROOT_INC}" \
			--arg path_val "${PIXI_GLOBAL_BIN}:\${PATH}" \
			'.argv = [$python, "-m", "ipykernel_launcher", "-f", "{connection_file}"] | .display_name = "Python (pixi global)" | .language = "python" | .metadata = {"debugger": true} | .env = {"C_INCLUDE_PATH": $inc, "CPLUS_INCLUDE_PATH": $inc, "PATH": $path_val}' \
			"${BASE_PY3_KERNEL_JSON}" >"${BASE_PY3_KERNEL_JSON}.tmp" &&
			mv "${BASE_PY3_KERNEL_JSON}.tmp" "${BASE_PY3_KERNEL_JSON}"
	else
		echo "WARNING: pixi-global kernel not installed; python3 stays the image's kernel" >&2
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

# Create bashrc_af file; root writes it, so a user's link at that path goes first.
bashrc_af_file="$NEW_HOME/.bashrc_af"
rm -f "$bashrc_af_file"

# sshd starts its shells with an empty environment: they get these values from this file.
for _var in NB_USER NB_UID NB_GID PYROSCOPE_SERVER PYROSCOPE_APP \
	DASK_GATEWAY__ADDRESS DASK_GATEWAY__PROXY_ADDRESS X509_CERT_DIR; do
	if [ -n "${!_var:-}" ]; then
		printf 'export %s=%q\n' "$_var" "${!_var}"
	fi
done >"$bashrc_af_file"

cat >>"$bashrc_af_file" <<'EOF'

# Under sshd, PATH holds only the system directories.
case ":${PATH}:" in
*:/opt/pixi/bin:*) ;;
*) export PATH="/usr/local/bin:/opt/npm-global/bin:/opt/pixi/.pixi/envs/base-env/bin:/opt/pixi/bin:${PATH}:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin" ;;
esac

export PIXI_HOME="/work/users/${NB_USER}/.pixi-home"
export PIXI_CACHE_DIR="/work/users/${NB_USER}/.pixi-cache/"

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

# Make .bashrc_af read-only for user (system-managed file)
chown root:root "$bashrc_af_file"
chmod 644 "$bashrc_af_file"

# Fresh homes and older images left these root-owned.
bashrc_file="$NEW_HOME/.bashrc"
bash_profile="$NEW_HOME/.bash_profile"
chown -h "$NB_USER:users" "$bashrc_file" "$bash_profile" 2>/dev/null || true

# Link bashrc_af into bashrc
extra_bashrc="source $NEW_HOME/.bashrc_af"
af_as_user grep -qxF "$extra_bashrc" "$bashrc_file" 2>/dev/null ||
	echo "$extra_bashrc" | af_as_user tee -a "$bashrc_file" >/dev/null ||
	echo "WARNING: could not add .bashrc_af to $bashrc_file" >&2

# Login shells (Jupyter terminals, sshd) read only this file. The .bashrc_af line marks a copy older images wrote.
if ! af_as_user test -e "$bash_profile" || af_as_user grep -qxF "$extra_bashrc" "$bash_profile" 2>/dev/null; then
	echo '[ -f ~/.bashrc ] && . ~/.bashrc' | af_as_user tee "$bash_profile" >/dev/null ||
		echo "WARNING: could not write $bash_profile" >&2
fi
