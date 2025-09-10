#!/bin/bash
set -euo pipefail

# ============================================================
# Conda env builder/updater (no deletes, no mamba)
# Fix: run conda as the target UID/GID (not root) even on NFS
# ============================================================

cleanup() {
	local exit_code=$?
	if [ $exit_code -ne 0 ] && [ -n "${WORK_DIR:-}" ] && [ -d "$WORK_DIR" ]; then
		echo "Script failed with exit code $exit_code, cleaning up work directory..."
		$RUN_AS_UID rm -rf "$WORK_DIR" 2>/dev/null || true
	fi
	exit $exit_code
}
trap cleanup EXIT

# -------------------------------
# args
# -------------------------------
if [ $# -lt 4 ] || [ $# -gt 5 ]; then
	echo "Usage: $0 <environment_name> <environment_directory> <environment_file> <location_root> [pip_uninstall_file]"
	exit 1
fi
ENV_NAME="$1"
ENV_DIR="$2"
ENV_FILE="$3"
LOCATION_ROOT="$4"
PIP_UNINSTALL_FILE="${5:-}"

echo "Building conda environment: $ENV_NAME at $LOCATION_ROOT"

# -------------------------------
# system deps (Rocky mirrors with fallbacks)
# -------------------------------
echo "Installing required packages..."
for mirror in "https://mirrors.rockylinux.org" "https://mirror.rockylinux.org" "https://dl.rockylinux.org"; do
	if dnf install -y git wget bzip2 sudo python3-pip which --nogpgcheck --setopt=mirrorlist="${mirror}/mirrorlist?arch=x86_64&repo=baseos-8" 2>/dev/null; then
		echo "Successfully installed packages using mirror: $mirror"
		break
	else
		echo "Failed to install packages using mirror: $mirror, trying next..."
	fi
done
if ! rpm -q git wget bzip2 sudo python3-pip which >/dev/null 2>&1; then
	echo "All mirrors failed, trying default dnf configuration..."
	dnf install -y git wget bzip2 sudo python3-pip which --nogpgcheck || {
		echo "ERROR: Failed to install required packages even with fallbacks"
		exit 1
	}
fi

pip3 install -q ldap3
if ! command -v pip >/dev/null 2>&1 && command -v pip3 >/dev/null 2>&1; then
	ln -sf "$(command -v pip3)" /usr/local/bin/pip
fi

# -------------------------------
# bootstrap conda (micromamba only for bootstrap; no mamba later)
# -------------------------------
echo "Installing Miniconda via micromamba..."
wget -qO /tmp/micromamba.tar.bz2 "https://micromamba.snakepit.net/api/micromamba/linux-64/latest"
tar -xvjf /tmp/micromamba.tar.bz2 --strip-components=1 bin/micromamba
chmod +x micromamba
./micromamba install --root-prefix="/opt/conda" --prefix="/opt/conda" --yes 'conda' 'pip' 'conda-env'
rm micromamba

ln -sf /opt/conda/bin/conda /usr/local/bin/conda
ln -sf /opt/conda/bin/python /usr/local/bin/python
ln -sf /opt/conda/bin/pip /usr/local/bin/pip
/opt/conda/bin/conda --version >/dev/null

if ! /opt/conda/bin/conda env --help >/dev/null 2>&1; then
	echo "'conda env' not available, installing conda-env..."
	/opt/conda/bin/conda install -y conda-env
fi

/opt/conda/bin/pip install -q ldap3

# -------------------------------
# LDAP lookup for target user
# -------------------------------
ldap_lookup() {
	local username="$1"
	/opt/conda/bin/python -c "
import ldap3, json
try:
    server = ldap3.Server('geddes-aux.rcac.purdue.edu', use_ssl=True, get_info='ALL')
    conn = ldap3.Connection(server, authentication='ANONYMOUS', version=3)
    conn.start_tls()
    conn.search('ou=People,dc=rcac,dc=purdue,dc=edu', '(uid=$username)',
                ldap3.SUBTREE, attributes=['uidNumber','gidNumber'])
    if conn.entries:
        d = json.loads(conn.response_to_json())['entries'][0]['attributes']
        uid = d.get('uidNumber', [''])[0] if isinstance(d.get('uidNumber'), list) else d.get('uidNumber','')
        gid = d.get('gidNumber', [''])[0] if isinstance(d.get('gidNumber'), list) else d.get('gidNumber','')
        print(f\"{uid}:{gid}\" if str(uid).isdigit() and str(gid).isdigit() else '')
    else:
        print('')
except Exception:
    print('')
"
}
TARGET_USERNAME="dkondra"
LDAP_RESULT=$(ldap_lookup "$TARGET_USERNAME" | tr -d ' \t\r\n')
if [ -n "$LDAP_RESULT" ] && [[ "$LDAP_RESULT" =~ ^[0-9]+:[0-9]+$ ]]; then
	TARGET_UID=${LDAP_RESULT%%:*}
	TARGET_GID=${LDAP_RESULT##*:}
	echo "Using LDAP UID:GID -> $TARGET_UID:$TARGET_GID"
else
	TARGET_UID="616617"
	TARGET_GID="18951"
	echo "LDAP unavailable, fallback UID:GID -> $TARGET_UID:$TARGET_GID"
fi
# Create group/user if needed
groupadd -g "$TARGET_GID" "$TARGET_USERNAME" 2>/dev/null || true
useradd -u "$TARGET_UID" -g "$TARGET_GID" -M -s /bin/bash "$TARGET_USERNAME" 2>/dev/null || true

# Convenience runner for simple filesystem ops
RUN_AS_UID=(sudo -H -u "$TARGET_USERNAME")

# -------------------------------
# per-run workspace / config
# -------------------------------
USER_TMP="/tmp/conda-env-builder-${TARGET_USERNAME}-${ENV_NAME}-$$"
USER_PKGS="${USER_TMP}/pkgs"
USER_ENVS="${USER_TMP}/envs"

"${RUN_AS_UID[@]}" rm -rf "$USER_TMP" 2>/dev/null || true
"${RUN_AS_UID[@]}" mkdir -p "$USER_PKGS" "$USER_ENVS" "$USER_TMP/pip-cache" "$USER_TMP/work" "$USER_TMP/conda-tmp" "$USER_TMP/env" "$USER_TMP/.cache/conda/proc" "$USER_TMP/.cache/conda/logs" "$USER_TMP/.cache/conda/notices"
chown -R "$TARGET_USERNAME:$TARGET_USERNAME" "$USER_TMP"
chmod -R 755 "$USER_TMP"

# .condarc with expanded absolute paths
"${RUN_AS_UID[@]}" bash -c "cat >'${USER_TMP}/.condarc' <<EOF
pkgs_dirs:
  - ${USER_PKGS}
envs_dirs:
  - ${USER_ENVS}
channels:
  - conda-forge
  - defaults
channel_priority: strict
repodata_fns:
  - current_repodata.json
use_lockfiles: false
always_copy: true
auto_activate_base: false
env_prompt: '({name})'
EOF"

# -------------------------------
# target paths / fetch env repo
# -------------------------------
ENV_PATH="${LOCATION_ROOT%/}/$ENV_NAME"
PARENT_DIR=$(dirname "$ENV_PATH")
[ -d "$PARENT_DIR" ] || "${RUN_AS_UID[@]}" mkdir -p "$PARENT_DIR"
[ -d "$ENV_PATH" ] || "${RUN_AS_UID[@]}" mkdir -p "$ENV_PATH"

## --- SMALL ADDITION: pick a pkgs dir on the SAME FS as ENV (to allow hardlinks) ---
# Default to the tmp-based pkgs dir (safe for NFS; uses copies).
PKGS_DIR_FOR_RUN="$USER_PKGS"
CONDA_ALWAYS_COPY_RUN="1" # copy by default (safer on network FS)

# Try a pkgs dir under the env's parent; if it's the same filesystem and not network-like,
# we allow hardlinks for speed (set CONDA_ALWAYS_COPY_RUN=0).
PKGS_SAMEFS_DIR="$(dirname "$ENV_PATH")/.conda-pkgs-$TARGET_USERNAME"
"${RUN_AS_UID[@]}" mkdir -p "$PKGS_SAMEFS_DIR" 2>/dev/null || true

# Compare filesystem IDs; enable hardlinks only when on the same local FS.
FSID_ENV=$(stat -fc %d "$ENV_PATH" 2>/dev/null || echo 0)
FSID_PKGS=$(stat -fc %d "$PKGS_SAMEFS_DIR" 2>/dev/null || echo 1)
FSTYPE_ENV=$(stat -f -c %T "$ENV_PATH" 2>/dev/null || echo unknown)
if [ -d "$PKGS_SAMEFS_DIR" ] && [ "$FSID_ENV" = "$FSID_PKGS" ] &&
	! [[ "$FSTYPE_ENV" =~ (nfs|cifs|smb|fuse|overlay) ]]; then
	PKGS_DIR_FOR_RUN="$PKGS_SAMEFS_DIR"
	CONDA_ALWAYS_COPY_RUN="0" # allow hardlinks (fast)
fi

echo "conda-env-builder: PKGS_DIR_FOR_RUN=${PKGS_DIR_FOR_RUN}"
echo "conda-env-builder: CONDA_ALWAYS_COPY_RUN=${CONDA_ALWAYS_COPY_RUN} (0=hardlink ok, 1=copy)"
## --- END ADDITION ---

BUILD_DIR="/tmp/conda-env-builds-$(date +%s)-$$"
"${RUN_AS_UID[@]}" find /tmp -maxdepth 1 -name "conda-env-builder-${TARGET_USERNAME}-*" -type d -mmin +10 -exec rm -rf {} \; 2>/dev/null || true
"${RUN_AS_UID[@]}" find /tmp -maxdepth 1 -name "conda-env-builds-*" -type d -mmin +10 -exec rm -rf {} \; 2>/dev/null || true
"${RUN_AS_UID[@]}" mkdir -p "$BUILD_DIR"
WORK_DIR="${BUILD_DIR}/work"
"${RUN_AS_UID[@]}" mkdir -p "$WORK_DIR"

"${RUN_AS_UID[@]}" git clone https://github.com/PurdueAF/purdue-af-conda-envs.git "$WORK_DIR"

if ! "${RUN_AS_UID[@]}" bash -c "cd '$WORK_DIR' && [ -d '${ENV_DIR}' ]"; then
	echo "ERROR: Environment directory ${ENV_DIR} not found!"
	exit 1
fi
ENV_YAML_PATH="${WORK_DIR}/${ENV_DIR}/${ENV_FILE}"
[ -f "$ENV_YAML_PATH" ] || {
	echo "ERROR: Environment file not found at $ENV_YAML_PATH"
	exit 1
}

ENV_YAML_COPY="${USER_TMP}/env/environment.yaml"
"${RUN_AS_UID[@]}" mkdir -p "$(dirname "$ENV_YAML_COPY")"
"${RUN_AS_UID[@]}" cp "$ENV_YAML_PATH" "$ENV_YAML_COPY"

# sanitize YAML (strip BOM/CR; keep spaces; replace tab with 2 spaces)
"${RUN_AS_UID[@]}" bash -c "
set -e
YFILE='$ENV_YAML_COPY'
# Strip UTF-8 BOM
if [ \"\$(head -c 3 \"\$YFILE\" 2>/dev/null)\" = \$'\xEF\xBB\xBF' ]; then
  tail -c +4 \"\$YFILE\" > \"\$YFILE.tmp\" && mv \"\$YFILE.tmp\" \"\$YFILE\"
fi
# Normalize CRLF -> LF
tr -d '\r' < \"\$YFILE\" > \"\$YFILE.tmp\" && mv \"\$YFILE.tmp\" \"\$YFILE\"
# Replace literal tabs (YAML forbids tabs for indentation)
if grep -qP '\t' \"\$YFILE\"; then sed -i $'s/\t/  /g' \"\$YFILE\"; fi
"

# -------------------------------
# run conda EXACTLY as the target user (not root)
# Use sudo -H -u USER -g "#GID" and a clean env (env -i).
# -------------------------------
run_conda_as_target() {
	# args: <config_file> <global_flags_string> <subcommand...>
	local cfg="$1"
	shift
	local global_flags="$1"
	shift
	# shellcheck disable=SC2086
	sudo -H -u "$TARGET_USERNAME" -g "#$TARGET_GID" env -i \
		HOME="$USER_TMP" USER="$TARGET_USERNAME" LOGNAME="$TARGET_USERNAME" \
		PATH="/opt/conda/bin:/usr/local/bin:/usr/bin:/bin" \
		CONDA_PKGS_DIRS="$PKGS_DIR_FOR_RUN" \
		CONDA_ENVS_PATH="$USER_ENVS" \
		CONDA_CONFIG_FILE="$cfg" PIP_CACHE_DIR="$USER_TMP/pip-cache" \
		TMPDIR="$USER_TMP/conda-tmp" TEMP="$USER_TMP/conda-tmp" TMP="$USER_TMP/conda-tmp" \
		CONDA_ALWAYS_COPY="$CONDA_ALWAYS_COPY_RUN" \
		CONDA_ALWAYS_YES="true" \
		/opt/conda/bin/conda $global_flags "$@"
}

conda_env_update() {
	local env_path="$1" yaml_path="$2" cfg="$3"
	run_conda_as_target "$cfg" "" env update --prefix "$env_path" --file "$yaml_path" -q
}

conda_env_create() {
	local env_path="$1" yaml_path="$2" cfg="$3"
	run_conda_as_target "$cfg" "" env create --prefix "$env_path" --file "$yaml_path" -q
}

# -------------------------------
# create or update (NEVER delete)
# -------------------------------
if [ -d "$ENV_PATH/conda-meta" ] && [ -f "$ENV_PATH/conda-meta/history" ]; then
	echo "Updating existing environment: $ENV_NAME"
	if conda_env_update "$ENV_PATH" "$ENV_YAML_COPY" "${USER_TMP}/.condarc"; then
		echo "✓ Environment updated"
	else
		echo "ERROR: Failed to update environment"
		exit 1
	fi
else
	echo "Creating new environment: $ENV_NAME"
	if conda_env_create "$ENV_PATH" "$ENV_YAML_COPY" "${USER_TMP}/.condarc"; then
		echo "✓ Environment created"
	else
		echo "ERROR: Failed to create environment"
		exit 1
	fi
fi

# -------------------------------
# install pyroscope-io via pip
# -------------------------------
echo "Installing pyroscope-io via pip..."
if [ -d "$ENV_PATH" ] && [ -d "$ENV_PATH/conda-meta" ]; then
	sudo -H -u "$TARGET_USERNAME" -g "#$TARGET_GID" env -i HOME="$USER_TMP" PATH="$ENV_PATH/bin:/opt/conda/bin:/usr/local/bin:/usr/bin:/bin" \
		python -m pip install pyroscope-io --quiet || {
		echo "⚠ Failed to install pyroscope-io via pip"
	}
	echo "✓ pyroscope-io installed via pip"
else
	echo "⚠ Environment not found, skipping pyroscope-io installation"
fi

# -------------------------------
# install sitecustomize.py for pyroscope monitoring
# -------------------------------
echo "Installing sitecustomize.py for pyroscope monitoring..."
if [ -d "$ENV_PATH" ] && [ -d "$ENV_PATH/conda-meta" ]; then
	# Construct site-packages path directly
	SITE_PACKAGES_DIR="${ENV_PATH}/lib/python*/site-packages"
	# Use shell globbing to find the actual python version directory
	for site_packages in ${SITE_PACKAGES_DIR}; do
		if [ -d "$site_packages" ]; then
			# Copy sitecustomize.py to site-packages
			"${RUN_AS_UID[@]}" cp "$(dirname "$0")/sitecustomize.py" "$site_packages/"
			echo "✓ sitecustomize.py installed to $site_packages"
			break
		fi
	done
	# Check if we found and installed to any site-packages directory
	if [ ! -f "${SITE_PACKAGES_DIR}/sitecustomize.py" ]; then
		echo "⚠ Could not find site-packages directory in environment"
	fi
else
	echo "⚠ Environment not found, skipping sitecustomize.py installation"
fi

# -------------------------------
# optional: pip uninstall list (run as target user)
# -------------------------------
if [ -n "$PIP_UNINSTALL_FILE" ] && [ -f "${WORK_DIR}/${ENV_DIR}/${PIP_UNINSTALL_FILE}" ]; then
	echo "Uninstalling packages from ${PIP_UNINSTALL_FILE}"
	while IFS= read -r package || [ -n "$package" ]; do
		package=$(echo "$package" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
		[[ -z "$package" || "$package" =~ ^[[:space:]]*# ]] && continue
		sudo -H -u "$TARGET_USERNAME" -g "#$TARGET_GID" env -i HOME="$USER_TMP" PATH="$ENV_PATH/bin:/opt/conda/bin:/usr/local/bin:/usr/bin:/bin" \
			python -m pip show "$package" >/dev/null 2>&1 || {
			echo "Skipping $package (not installed)"
			continue
		}
		echo "Uninstalling $package..."
		sudo -H -u "$TARGET_USERNAME" -g "#$TARGET_GID" env -i HOME="$USER_TMP" PATH="$ENV_PATH/bin:/opt/conda/bin:/usr/local/bin:/usr/bin:/bin" \
			python -m pip uninstall "$package" -y || echo "⚠ Failed to uninstall $package"
	done <"${WORK_DIR}/${ENV_DIR}/${PIP_UNINSTALL_FILE}"
	echo "Pip uninstall completed"
fi

# -------------------------------
# cleanup & verification
# -------------------------------
"${RUN_AS_UID[@]}" rm -rf "$WORK_DIR"
"${RUN_AS_UID[@]}" find "$BUILD_DIR" -name "*.tmp" -delete 2>/dev/null || true
"${RUN_AS_UID[@]}" find "$BUILD_DIR" -name "*.lock" -delete 2>/dev/null || true
"${RUN_AS_UID[@]}" find "$BUILD_DIR" -name "*.cache" -delete 2>/dev/null || true
"${RUN_AS_UID[@]}" find /tmp -maxdepth 1 -name "conda-env-builder-${TARGET_USERNAME}-*" -type d -mmin +5 -exec rm -rf {} \; 2>/dev/null || true
"${RUN_AS_UID[@]}" find /tmp -maxdepth 1 -name "conda-env-builds-*" -type d -mmin +5 -exec rm -rf {} \; 2>/dev/null || true

# Verify as the target user (to catch NFS root-squash issues)
if [ -d "$ENV_PATH" ] && [ -d "$ENV_PATH/conda-meta" ]; then
	if sudo -H -u "$TARGET_USERNAME" -g "#$TARGET_GID" env -i HOME="$USER_TMP" PATH="$ENV_PATH/bin:/usr/bin:/bin" python --version >/dev/null 2>&1; then
		echo "✓ Environment verification passed"
	else
		echo "⚠ Environment verification failed (permission or PATH issue)"
	fi
else
	echo "⚠ Environment verification failed - missing required directories"
fi

echo "✓ Environment build completed: $ENV_NAME"
