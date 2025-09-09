#!/bin/bash
set -euo pipefail

# -------------------------------
# cleanup on exit
# -------------------------------
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

pip3 install ldap3
if ! command -v pip >/dev/null 2>&1 && command -v pip3 >/dev/null 2>&1; then
	ln -sf "$(command -v pip3)" /usr/local/bin/pip
fi

# -------------------------------
# bootstrap conda (micromamba only for bootstrap; do NOT use mamba later)
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

echo "Installing ldap3 into base..."
/opt/conda/bin/pip install ldap3

# -------------------------------
# ldap lookup -> run as target user
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
	echo "Found LDAP info for '$TARGET_USERNAME'"
	TARGET_UID=${LDAP_RESULT%%:*}
	TARGET_GID=${LDAP_RESULT##*:}
else
	echo "LDAP lookup failed; using fallback"
	TARGET_UID="616617"
	TARGET_GID="18951"
fi
groupadd -g "$TARGET_GID" "$TARGET_USERNAME" 2>/dev/null || true
useradd -u "$TARGET_UID" -g "$TARGET_GID" -M -s /bin/bash "$TARGET_USERNAME" 2>/dev/null || true
RUN_AS_UID=(sudo -E -u "$TARGET_USERNAME")

# -------------------------------
# per-run workspace
# -------------------------------
USER_TMP="/tmp/conda-env-builder-${TARGET_USERNAME}-${ENV_NAME}-$$"
USER_PKGS="${USER_TMP}/pkgs"
USER_ENVS="${USER_TMP}/envs"

$RUN_AS_UID rm -rf "$USER_TMP" 2>/dev/null || true
$RUN_AS_UID mkdir -p "$USER_PKGS" "$USER_ENVS" "$USER_TMP/pip-cache" "$USER_TMP/work" "$USER_TMP/conda-tmp" "$USER_TMP/env" "$USER_TMP/.cache/conda/proc" "$USER_TMP/.cache/conda/logs" "$USER_TMP/.cache/conda/notices"
chown -R "$TARGET_USERNAME:$TARGET_USERNAME" "$USER_TMP"
chmod -R 755 "$USER_TMP"

# .condarc (EXPANDED paths; simple, stable)
$RUN_AS_UID bash -c "cat >'${USER_TMP}/.condarc' <<EOF
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

# basic conda sanity
$RUN_AS_UID bash -c "
export CONDA_PKGS_DIRS='${USER_PKGS}'
export CONDA_ENVS_PATH='${USER_ENVS}'
export CONDA_CONFIG_FILE='${USER_TMP}/.condarc'
export PIP_CACHE_DIR='${USER_TMP}/pip-cache'
export TMPDIR='${USER_TMP}/conda-tmp'
export TEMP='${USER_TMP}/conda-tmp'
export TMP='${USER_TMP}/conda-tmp'
export CONDA_ALWAYS_COPY='1'
export PATH='/opt/conda/bin:/usr/local/bin:/usr/bin:/bin'
/opt/conda/bin/conda --version >/dev/null
"

# -------------------------------
# paths / repo fetch
# -------------------------------
ENV_PATH="${LOCATION_ROOT%/}/$ENV_NAME"

PARENT_DIR=$(dirname "$ENV_PATH")
[ -d "$PARENT_DIR" ] || $RUN_AS_UID mkdir -p "$PARENT_DIR"
[ -d "$ENV_PATH" ] || $RUN_AS_UID mkdir -p "$ENV_PATH"

BUILD_DIR="/tmp/conda-env-builds-$(date +%s)-$$"
$RUN_AS_UID find /tmp -maxdepth 1 -name "conda-env-builder-${TARGET_USERNAME}-*" -type d -mmin +10 -exec rm -rf {} \; 2>/dev/null || true
$RUN_AS_UID find /tmp -maxdepth 1 -name "conda-env-builds-*" -type d -mmin +10 -exec rm -rf {} \; 2>/dev/null || true
$RUN_AS_UID mkdir -p "$BUILD_DIR"
chown -R "$TARGET_USERNAME:$TARGET_USERNAME" "$BUILD_DIR"
WORK_DIR="${BUILD_DIR}/work"
$RUN_AS_UID mkdir -p "$WORK_DIR"

$RUN_AS_UID git clone https://github.com/PurdueAF/purdue-af-conda-envs.git "$WORK_DIR"

if ! $RUN_AS_UID bash -c "cd '$WORK_DIR' && [ -d '${ENV_DIR}' ]"; then
	echo "ERROR: Environment directory ${ENV_DIR} not found!"
	exit 1
fi
ENV_YAML_PATH="${WORK_DIR}/${ENV_DIR}/${ENV_FILE}"
[ -f "$ENV_YAML_PATH" ] || {
	echo "ERROR: Environment file not found at $ENV_YAML_PATH"
	exit 1
}

ENV_YAML_COPY="${USER_TMP}/env/environment.yaml"
$RUN_AS_UID mkdir -p "$(dirname "$ENV_YAML_COPY")"
$RUN_AS_UID cp "$ENV_YAML_PATH" "$ENV_YAML_COPY"

# --- sanitize YAML safely: remove BOM/CR, warn on tabs (replace with spaces) ---
$RUN_AS_UID bash -c "
set -e
YFILE='$ENV_YAML_COPY'
# Strip BOM if present
if [ \"\$(head -c 3 \"\$YFILE\" 2>/dev/null)\" = \$'\xEF\xBB\xBF' ]; then
  tail -c +4 \"\$YFILE\" > \"\$YFILE.tmp\" && mv \"\$YFILE.tmp\" \"\$YFILE\"
fi
# Normalize line endings
tr -d '\r' < \"\$YFILE\" > \"\$YFILE.tmp\" && mv \"\$YFILE.tmp\" \"\$YFILE\"
# Detect tabs (YAML forbids tabs for indentation). Replace with two spaces.
if grep -qP '\t' \"\$YFILE\"; then
  echo 'Warning: tabs found in YAML; replacing with spaces' 1>&2
  sed -i $'s/\t/  /g' \"\$YFILE\"
fi
"

echo "=== DEBUG: YAML file head ==="
head -20 "$ENV_YAML_COPY" || true
echo "=== END DEBUG ==="

# -------------------------------
# helpers: no-op check, update/create
# IMPORTANT: use GLOBAL flags (before 'env') for --offline/--dry-run/--yes
# and DO NOT use unsupported flags for 'env update' (e.g., --freeze-installed).
# -------------------------------
check_env_needs_update() {
	local env_path="$1" yaml_path="$2"
	local out="$USER_TMP/conda_dry_run.log" err="$USER_TMP/conda_dry_run_error.log"

	if $RUN_AS_UID bash -c "
export CONDA_PKGS_DIRS='${USER_PKGS}'
export CONDA_ENVS_PATH='${USER_ENVS}'
export CONDA_CONFIG_FILE='${USER_TMP}/.condarc'
export PIP_CACHE_DIR='${USER_TMP}/pip-cache'
export TMPDIR='${USER_TMP}/conda-tmp'
export TEMP='${USER_TMP}/conda-tmp'
export TMP='${USER_TMP}/conda-tmp'
export CONDA_ALWAYS_COPY='1'
export PATH='/opt/conda/bin:/usr/local/bin:/usr/bin:/bin'
/opt/conda/bin/conda --offline --dry-run env update --prefix '$env_path' --file '$yaml_path'
" >"$out" 2>"$err"; then
		if grep -qE "All requested packages already installed|Transaction will be empty|Nothing to do" "$out"; then
			echo "No changes detected; skipping update."
			return 1
		fi
		echo "Changes detected, proceeding with update..."
		return 0
	else
		echo "Dry run failed (likely missing cached artifacts); will try real update."
		return 0
	fi
}

update_env() {
	local env_path="$1" yaml_path="$2"
	echo "Updating environment at $env_path..."

	# Attempt offline first (global flags BEFORE 'env')
	if $RUN_AS_UID bash -c "
export CONDA_PKGS_DIRS='${USER_PKGS}'
export CONDA_ENVS_PATH='${USER_ENVS}'
export CONDA_CONFIG_FILE='${USER_TMP}/.condarc'
export PIP_CACHE_DIR='${USER_TMP}/pip-cache'
export TMPDIR='${USER_TMP}/conda-tmp'
export TEMP='${USER_TMP}/conda-tmp'
export TMP='${USER_TMP}/conda-tmp'
export CONDA_ALWAYS_COPY='1'
export PATH='/opt/conda/bin:/usr/local/bin:/usr/bin:/bin'
/opt/conda/bin/conda --yes --offline env update --prefix '$env_path' --file '$yaml_path'
"; then
		echo "✓ Environment updated successfully (offline)"
		return 0
	else
		echo "Offline update failed, trying online update..."
		if $RUN_AS_UID bash -c "
export CONDA_PKGS_DIRS='${USER_PKGS}'
export CONDA_ENVS_PATH='${USER_ENVS}'
export CONDA_CONFIG_FILE='${USER_TMP}/.condarc'
export PIP_CACHE_DIR='${USER_TMP}/pip-cache'
export TMPDIR='${USER_TMP}/conda-tmp'
export TEMP='${USER_TMP}/conda-tmp'
export TMP='${USER_TMP}/conda-tmp'
export CONDA_ALWAYS_COPY='1'
export PATH='/opt/conda/bin:/usr/local/bin:/usr/bin:/bin'
/opt/conda/bin/conda --yes env update --prefix '$env_path' --file '$yaml_path'
"; then
			echo "✓ Environment updated successfully (online)"
			return 0
		else
			echo "ERROR: Failed to update environment"
			return 1
		fi
	fi
}

create_env() {
	local env_path="$1" yaml_path="$2"
	echo "Creating environment at $env_path..."
	if $RUN_AS_UID bash -c "
export CONDA_PKGS_DIRS='${USER_PKGS}'
export CONDA_ENVS_PATH='${USER_ENVS}'
export CONDA_CONFIG_FILE='${USER_TMP}/.condarc'
export PIP_CACHE_DIR='${USER_TMP}/pip-cache'
export TMPDIR='${USER_TMP}/conda-tmp'
export TEMP='${USER_TMP}/conda-tmp'
export TMP='${USER_TMP}/conda-tmp'
export CONDA_ALWAYS_COPY='1'
export PATH='/opt/conda/bin:/usr/local/bin:/usr/bin:/bin'
/opt/conda/bin/conda --yes env create --prefix '$env_path' --file '$yaml_path'
"; then
		echo "✓ Environment created successfully"
		return 0
	else
		echo "ERROR: Failed to create environment"
		return 1
	fi
}

# -------------------------------
# create or update (NEVER delete)
# -------------------------------
if [ -d "$ENV_PATH/conda-meta" ] && [ -f "$ENV_PATH/conda-meta/history" ]; then
	echo "Updating existing environment: $ENV_NAME"
	if check_env_needs_update "$ENV_PATH" "$ENV_YAML_COPY"; then
		update_env "$ENV_PATH" "$ENV_YAML_COPY" || exit 1
	fi
else
	echo "Creating new environment: $ENV_NAME"
	create_env "$ENV_PATH" "$ENV_YAML_COPY" || exit 1
fi

# -------------------------------
# optional: pip uninstall list
# -------------------------------
if [ -n "$PIP_UNINSTALL_FILE" ] && [ -f "${WORK_DIR}/${ENV_DIR}/${PIP_UNINSTALL_FILE}" ]; then
	echo "Uninstalling packages from ${PIP_UNINSTALL_FILE}"
	while IFS= read -r package || [ -n "$package" ]; do
		package=$(echo "$package" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
		[[ -z "$package" || "$package" =~ ^[[:space:]]*# ]] && continue
		if $RUN_AS_UID bash -c "export PATH='$ENV_PATH/bin:/opt/conda/bin:/usr/local/bin:/usr/bin:/bin'; python -m pip show '$package' >/dev/null 2>&1"; then
			echo "Uninstalling $package..."
			$RUN_AS_UID bash -c "export PATH='$ENV_PATH/bin:/opt/conda/bin:/usr/local/bin:/usr/bin:/bin'; python -m pip uninstall '$package' -y" || echo "⚠ Failed to uninstall $package"
		else
			echo "Skipping $package (not installed)"
		fi
	done <"${WORK_DIR}/${ENV_DIR}/${PIP_UNINSTALL_FILE}"
	echo "Pip uninstall completed"
fi

# -------------------------------
# cleanup & verification
# -------------------------------
$RUN_AS_UID rm -rf "$WORK_DIR"
$RUN_AS_UID find "$BUILD_DIR" -name "*.tmp" -delete 2>/dev/null || true
$RUN_AS_UID find "$BUILD_DIR" -name "*.lock" -delete 2>/dev/null || true
$RUN_AS_UID find "$BUILD_DIR" -name "*.cache" -delete 2>/dev/null || true
$RUN_AS_UID find /tmp -maxdepth 1 -name "conda-env-builder-${TARGET_USERNAME}-*" -type d -mmin +5 -exec rm -rf {} \; 2>/dev/null || true
$RUN_AS_UID find /tmp -maxdepth 1 -name "conda-env-builds-*" -type d -mmin +5 -exec rm -rf {} \; 2>/dev/null || true

if [ -d "$ENV_PATH" ] && [ -d "$ENV_PATH/conda-meta" ]; then
	if [ -x "$ENV_PATH/bin/python" ] && $RUN_AS_UID bash -c "export PATH='$ENV_PATH/bin:/opt/conda/bin:/usr/local/bin:/usr/bin:/bin'; python --version" >/dev/null 2>&1; then
		echo "✓ Environment verification passed"
	else
		echo "⚠ Environment verification failed"
	fi
else
	echo "⚠ Environment verification failed - missing required directories"
fi

echo "✓ Environment build completed: $ENV_NAME"
