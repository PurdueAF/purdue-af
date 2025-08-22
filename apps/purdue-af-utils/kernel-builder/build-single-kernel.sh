#!/bin/bash

set -e

# Function to clean up on exit
cleanup() {
	local exit_code=$?
	if [ $exit_code -ne 0 ] && [ -n "$WORK_DIR" ] && [ -d "$WORK_DIR" ]; then
		echo "Script failed with exit code $exit_code, cleaning up work directory..."
		$RUN_AS_UID rm -rf "$WORK_DIR" 2>/dev/null || true
	fi
	exit $exit_code
}

# Set trap to clean up on exit
trap cleanup EXIT

# Check arguments
if [ $# -lt 4 ] || [ $# -gt 5 ]; then
	echo "Usage: $0 <environment_name> <environment_directory> <environment_file> <location_root> [pip_uninstall_file]"
	exit 1
fi

ENV_NAME="$1"
ENV_DIR="$2"
ENV_FILE="$3"
LOCATION_ROOT="$4"
PIP_UNINSTALL_FILE="$5"

echo "Building kernel: $ENV_NAME at $LOCATION_ROOT"

# Install required packages
dnf install -y git wget bzip2 sudo python3-pip --nogpgcheck

# Install ldap3 for LDAP lookups
pip3 install ldap3

# Download and install micromamba
echo "Installing micromamba..."
wget -qO /tmp/micromamba.tar.bz2 "https://micromamba.snakepit.net/api/micromamba/linux-64/latest"
tar -xvjf /tmp/micromamba.tar.bz2 --strip-components=1 bin/micromamba
mv micromamba /usr/local/bin/
chmod +x /usr/local/bin/micromamba

# LDAP lookup function
ldap_lookup() {
	local username="$1"
	python3 -c "
import ldap3
import json

try:
    server = ldap3.Server('geddes-aux.rcac.purdue.edu', use_ssl=True, get_info='ALL')
    conn = ldap3.Connection(server, authentication='ANONYMOUS', version=3)
    conn.start_tls()
    
    conn.search(
        search_base='ou=People,dc=rcac,dc=purdue,dc=edu',
        search_filter='(uid=$username)',
        search_scope=ldap3.SUBTREE,
        attributes=['uidNumber', 'gidNumber']
    )
    
    if conn.entries:
        result = json.loads(conn.response_to_json())['entries'][0]['attributes']
        uid_number = result.get('uidNumber', [''])[0] if isinstance(result.get('uidNumber'), list) else result.get('uidNumber', '')
        gid_number = result.get('gidNumber', [''])[0] if isinstance(result.get('gidNumber'), list) else result.get('gidNumber', '')
        
        if uid_number and gid_number and str(uid_number).isdigit() and str(gid_number).isdigit():
            print(f'{uid_number}:{gid_number}')
        else:
            print('')
    else:
        print('')
except Exception:
    print('')
"
}

# Create user for running mamba
TARGET_USERNAME="dkondra"

# Look up UID/GID from LDAP
LDAP_RESULT=$(ldap_lookup "$TARGET_USERNAME" | tr -d ' \t\r\n')

if [ -n "$LDAP_RESULT" ] && [[ "$LDAP_RESULT" =~ ^[0-9]+:[0-9]+$ ]]; then
	echo "Found LDAP info for username '$TARGET_USERNAME'"
	# Parse UID:GID from LDAP result
	TARGET_UID=$(echo "$LDAP_RESULT" | cut -d: -f1)
	TARGET_GID=$(echo "$LDAP_RESULT" | cut -d: -f2)
	echo "UID: $TARGET_UID, GID: $TARGET_GID"

	# Create group and user with LDAP values
	groupadd -g "$TARGET_GID" "$TARGET_USERNAME" || true
	useradd -u "$TARGET_UID" -g "$TARGET_GID" -M -s /bin/bash "$TARGET_USERNAME" || true
	RUN_AS_UID=(sudo -E -u "$TARGET_USERNAME")
else
	echo "Could not lookup username '$TARGET_USERNAME' from LDAP or invalid format: '$LDAP_RESULT', using fallback"
	# Fallback to hardcoded values
	TARGET_UID="616617"
	TARGET_GID="18951"

	# Create group and user
	groupadd -g "$TARGET_GID" "$TARGET_USERNAME" || true
	useradd -u "$TARGET_UID" -g "$TARGET_GID" -M -s /bin/bash "$TARGET_USERNAME" || true
	RUN_AS_UID=(sudo -E -u "$TARGET_USERNAME")
fi

# Set up conda paths for the target user - include environment name and process ID for uniqueness
USER_TMP="/tmp/kernel-builder-${TARGET_USERNAME}-${ENV_NAME}-$$"
USER_CONDA="${USER_TMP}/conda"
USER_PKGS="${USER_TMP}/pkgs"
USER_ENVS="${USER_TMP}/envs"
USER_CACHE="${USER_TMP}/cache"
USER_CACHE_PKGS="${USER_TMP}/cache-pkgs"
USER_CACHE_ENVS="${USER_TMP}/cache-envs"

echo "Using temporary paths for user $TARGET_USERNAME"

# Clean up any existing directories to avoid permission issues
rm -rf "$USER_TMP" 2>/dev/null || true

# Create user-specific directories with proper ownership
mkdir -p "$USER_CONDA" "$USER_CACHE" "$USER_PKGS" "$USER_ENVS" "$USER_CACHE_PKGS" "$USER_CACHE_ENVS" "$USER_TMP/pip-cache" "$USER_TMP/xdg-cache"
chown -R "$TARGET_USERNAME:$TARGET_USERNAME" "$USER_TMP"

# Ensure proper permissions on cache directories
chmod 755 "$USER_TMP" "$USER_CONDA" "$USER_CACHE" "$USER_PKGS" "$USER_ENVS" "$USER_CACHE_PKGS" "$USER_CACHE_ENVS" "$USER_TMP/pip-cache" "$USER_TMP/xdg-cache"

# Create the specific cache subdirectories that conda/micromamba expects
$RUN_AS_UID bash -c "
mkdir -p '$USER_PKGS/cache' '$USER_PKGS/envs' '$USER_PKGS/pkgs' 2>/dev/null || true
mkdir -p '$USER_ENVS/.conda' 2>/dev/null || true
mkdir -p '$USER_PKGS/cache/repodata' 2>/dev/null || true
mkdir -p '$USER_PKGS/cache/pkgs' 2>/dev/null || true
"
chown -R "$TARGET_USERNAME:$TARGET_USERNAME" "$USER_PKGS" "$USER_ENVS"
chmod -R 755 "$USER_PKGS" "$USER_ENVS"

# Clean up any stale lockfiles
find "$USER_TMP" -name "*.lock" -delete 2>/dev/null || true

# Create conda configuration file
cat >"${USER_TMP}/.condarc" <<EOF
pkgs_dirs:
  - $USER_PKGS
envs_dirs:
  - $USER_ENVS
channels:
  - conda-forge
  - defaults
channel_priority: flexible
use_lockfiles: false
# Explicitly set cache directories
pkg_cache_dir: $USER_PKGS/cache
env_cache_dir: $USER_ENVS/.conda
# Disable aggressive caching that might cause issues
aggressive_update_packages: []
# Set package cache timeout
pkg_cache_timeout: 0
EOF

chown "$TARGET_USERNAME:$TARGET_USERNAME" "${USER_TMP}/.condarc"

# Verify micromamba is working
$RUN_AS_UID bash -c "
export MAMBA_ROOT_PREFIX='$USER_CONDA'
export CONDA_PKGS_DIRS='$USER_PKGS'
export CONDA_ENVS_PATH='$USER_ENVS'
export CONDA_PREFIX='$USER_CONDA'
export CONDA_DEFAULT_ENV='$USER_CONDA'
export CONDA_CONFIG_FILE='${USER_TMP}/.condarc'
export MAMBA_PKGS_DIRS='$USER_PKGS'
export MAMBA_ENVS_PATH='$USER_ENVS'
export MAMBA_CACHE_DIR='$USER_PKGS/cache'
export PIP_CACHE_DIR='$USER_TMP/pip-cache'
export XDG_CACHE_HOME='$USER_TMP/xdg-cache'
export PIP_USER=no
export PIP_NO_CACHE_DIR=0
/usr/local/bin/micromamba --version
"

# Set up paths
ENV_PATH="${LOCATION_ROOT}/$ENV_NAME"
ENV_DIR_NAME="$ENV_DIR"
ENV_FILE_NAME="$ENV_FILE"
ENV_YAML_PATH=""

echo "Building environment '$ENV_NAME' at '$ENV_PATH'"

# Ensure the target environment directory exists
if [ ! -d "$ENV_PATH" ]; then
	$RUN_AS_UID mkdir -p "$ENV_PATH"
fi

# Check directory ownership for Depot mount
if [ "$(stat -c '%U' "$ENV_PATH")" != "$TARGET_USERNAME" ]; then
	echo "WARNING: Directory is not owned by $TARGET_USERNAME"
fi

# Function to run micromamba
run_micromamba() {
	if "${RUN_AS_UID[@]}" bash -c "
export MAMBA_ROOT_PREFIX='$USER_CONDA'
export CONDA_PKGS_DIRS='$USER_PKGS'
export CONDA_ENVS_PATH='$USER_ENVS'
export CONDA_PREFIX='$USER_CONDA'
export CONDA_DEFAULT_ENV='$USER_CONDA'
export CONDA_CONFIG_FILE='${USER_TMP}/.condarc'
export MAMBA_PKGS_DIRS='$USER_PKGS'
export MAMBA_ENVS_PATH='$USER_ENVS'
export MAMBA_CACHE_DIR='$USER_PKGS/cache'
export PIP_CACHE_DIR='$USER_TMP/pip-cache'
export XDG_CACHE_HOME='$USER_TMP/xdg-cache'
export PIP_USER=no
export PIP_NO_CACHE_DIR=0
/usr/local/bin/micromamba \"\$@\"
" "$@"; then
		return 0
	else
		return $?
	fi
}

# Use /tmp for build directory - no permission issues, use timestamp and PID for uniqueness
BUILD_DIR="/tmp/kernel-builds-$(date +%s)-$$"

# Clean up old temporary directories from previous runs
find /tmp -maxdepth 1 -name "kernel-builder-${TARGET_USERNAME}-*" -type d -mmin +10 -exec rm -rf {} \; 2>/dev/null || true
find /tmp -maxdepth 1 -name "kernel-builds-*" -type d -mmin +10 -exec rm -rf {} \; 2>/dev/null || true

# Create build directory as target user
$RUN_AS_UID mkdir -p "$BUILD_DIR" || true

# Create work directory - use a simple name to avoid path confusion
WORK_DIR="${BUILD_DIR}/work"
$RUN_AS_UID mkdir -p "$WORK_DIR" || true

# Ensure the work directory has correct ownership and permissions
chown -R "$TARGET_USERNAME:$TARGET_USERNAME" "$WORK_DIR"
chmod -R 755 "$WORK_DIR"

# Clone the repository
$RUN_AS_UID git clone https://github.com/PurdueAF/purdue-af-conda-envs.git "$WORK_DIR"

# Verify the environment directory and file exist
if ! $RUN_AS_UID bash -c "cd '$WORK_DIR' && [ -d '${ENV_DIR_NAME}' ]"; then
	echo "ERROR: Environment directory ${ENV_DIR_NAME} not found!"
	exit 1
fi

ENV_YAML_PATH="${WORK_DIR}/${ENV_DIR_NAME}/${ENV_FILE_NAME}"
if [ ! -f "$ENV_YAML_PATH" ]; then
	echo "ERROR: Environment file not found at $ENV_YAML_PATH"
	exit 1
fi

# Check if environment already exists and is valid
if [ -d "$ENV_PATH" ]; then
	echo "Updating existing environment: $ENV_NAME"
	if run_micromamba env update -f "$ENV_YAML_PATH" -p "$ENV_PATH" -y; then
		echo "Successfully updated environment: $ENV_NAME"
	else
		echo "ERROR: Failed to update environment: $ENV_NAME"
		exit 1
	fi
else
	echo "Creating new environment: $ENV_NAME"
	if run_micromamba env create -f "$ENV_YAML_PATH" -p "$ENV_PATH" -y; then
		echo "Successfully created environment: $ENV_NAME"
	else
		echo "ERROR: Failed to create environment: $ENV_NAME"
		exit 1
	fi
fi

# Handle pip uninstall if specified
if [ -n "$PIP_UNINSTALL_FILE" ] && [ -f "${WORK_DIR}/${ENV_DIR_NAME}/${PIP_UNINSTALL_FILE}" ]; then
	echo "Uninstalling packages from ${PIP_UNINSTALL_FILE}"
	run_micromamba run -p "$ENV_PATH" python -m pip uninstall -r "${WORK_DIR}/${ENV_DIR_NAME}/${PIP_UNINSTALL_FILE}" -y
fi

# Clean up
$RUN_AS_UID rm -rf "$WORK_DIR"

# Final cleanup of temporary directories
find /tmp -maxdepth 1 -name "kernel-builder-${TARGET_USERNAME}-*" -type d -mmin +5 -exec rm -rf {} \; 2>/dev/null || true
find /tmp -maxdepth 1 -name "kernel-builds-*" -type d -mmin +5 -exec rm -rf {} \; 2>/dev/null || true

echo "Kernel building completed for environment: $ENV_NAME"
