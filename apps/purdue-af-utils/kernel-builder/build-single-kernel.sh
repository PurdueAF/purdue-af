#!/bin/bash

set -e

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

# Use micromamba to install mamba
echo "Installing mamba via micromamba..."
micromamba install --yes --root-prefix=/tmp/conda --prefix=/tmp/conda mamba
export PATH="/tmp/conda/bin:$PATH"

# Set up paths
ENV_PATH="${LOCATION_ROOT}/$ENV_NAME"
ENV_DIR_NAME="$ENV_DIR"
ENV_FILE_NAME="$ENV_FILE"
ENV_YAML_PATH=""

# LDAP lookup function
ldap_lookup() {
	local username="$1"
	local url="geddes-aux.rcac.purdue.edu"
	local baseDN="ou=People,dc=rcac,dc=purdue,dc=edu"
	local search_filter="(uid={0}*)"
	local attrs=('uidNumber','gidNumber')

	python3 -c "
import ldap3
import json
try:
    server = ldap3.Server('$url', use_ssl=True, get_info='ALL')
    conn = ldap3.Connection(server, version=3, authentication='ANONYMOUS')
    conn.start_tls()
    
    # Use the same search filter format as dask-gateway
    search_filter = '(uid=$username*)'
    print(f'Searching with filter: {search_filter}')
    
    conn.search(
        search_base='$baseDN',
        search_filter=search_filter,
        search_scope=ldap3.SUBTREE,
        attributes=$attrs
    )
    
    print(f'Search result: {conn.result}')
    print(f'Connection: {conn}')
    
    if conn.entries:
        print(f'Found {len(conn.entries)} entries')
        # Use the same parsing approach as dask-gateway
        ldap_result_id = json.loads(conn.response_to_json())
        print(f'LDAP response: {ldap_result_id}')
        
        if ldap_result_id['entries']:
            result = ldap_result_id['entries'][0]['attributes']
            uid_number = result['uidNumber']
            gid_number = result['gidNumber']
            print(f'{uid_number}:{gid_number}')
        else:
            print('')
    else:
        print('No entries found')
        print('')
except Exception as e:
    print(f'LDAP error: {e}')
    import traceback
    traceback.print_exc()
    print('')
"
}

# Create user for running mamba
TARGET_USERNAME="dkondra"
LDAP_RESULT=$(ldap_lookup "$TARGET_USERNAME")

if [ -n "$LDAP_RESULT" ]; then
	echo "Found LDAP info for username '$TARGET_USERNAME'"
	# Parse UID:GID from LDAP result
	TARGET_UID=$(echo "$LDAP_RESULT" | cut -d: -f1)
	TARGET_GID=$(echo "$LDAP_RESULT" | cut -d: -f1)
	echo "UID: $TARGET_UID, GID: $TARGET_GID"

	# Create group and user
	groupadd -g "$TARGET_GID" "$TARGET_USERNAME" || true
	useradd -u "$TARGET_UID" -g "$TARGET_GID" -M -s /bin/bash "$TARGET_USERNAME" || true
	RUN_AS_UID=(sudo -E -u "$TARGET_USERNAME")
else
	echo "Could not lookup username '$TARGET_USERNAME' from LDAP, using fallback"
	# Fallback to a default user
	TARGET_UID="616617"
	groupadd -g "$TARGET_UID" "user$TARGET_UID" || true
	useradd -u "$TARGET_UID" -g "$TARGET_UID" -M -s /bin/bash "user$TARGET_UID" || true
	RUN_AS_UID=(sudo -E -u "user$TARGET_UID")
fi

export MAMBA_ROOT_PREFIX=/tmp/conda
export XDG_CACHE_HOME=/tmp/.cache

# Function to run mamba
run_mamba() {
	echo "Running mamba (argv): /tmp/conda/bin/mamba $*"
	echo "Verifying file still exists:"
	ls -la "$(dirname "$ENV_YAML_PATH")" || echo "Cannot list directory"
	if "${RUN_AS_UID[@]}" /tmp/conda/bin/mamba "$@"; then
		echo "Mamba command succeeded"
		return 0
	else
		rc=$?
		echo "Mamba command failed, exit code: $rc"
		return $rc
	fi
}

# Create build directory
BUILD_DIR="$LOCATION_ROOT/.build-temp"

# Debug: Check mount status
echo "Checking mount status..."
mount | grep depot || echo "No depot mounts found"
ls -la /depot/cms 2>/dev/null || echo "Cannot access /depot/cms"
echo "Location root: $LOCATION_ROOT"
echo "Build dir: $BUILD_DIR"

$RUN_AS_UID mkdir -p "$BUILD_DIR" || true

# Create work directory
WORK_DIR="${BUILD_DIR}/${ENV_NAME}-$(date +%s)-$$"
$RUN_AS_UID mkdir -p "$WORK_DIR" || true

# Clone the repository
echo "Cloning repository..."
$RUN_AS_UID git clone https://github.com/PurdueAF/purdue-af-conda-envs.git "$WORK_DIR"
cd "$WORK_DIR"

# Debug: Check what was cloned
echo "Repository contents after clone:"
ls -la
echo "Looking for directory: ${ENV_DIR_NAME}"
if [ -d "${ENV_DIR_NAME}" ]; then
	echo "Directory ${ENV_DIR_NAME} exists, contents:"
	ls -la "${ENV_DIR_NAME}"
else
	echo "Directory ${ENV_DIR_NAME} does not exist!"
	echo "Available directories:"
	find . -maxdepth 1 -type d | head -20
fi

# Set the environment file path
ENV_YAML_PATH="${WORK_DIR}/${ENV_DIR_NAME}/${ENV_FILE_NAME}"
echo "Environment file path: $ENV_YAML_PATH"

# Validate that the environment file exists
if [ ! -f "$ENV_YAML_PATH" ]; then
	echo "ERROR: Environment file not found at $ENV_YAML_PATH"
	echo "Current working directory: $(pwd)"
	echo "Work directory: $WORK_DIR"
	echo "Environment directory: ${ENV_DIR_NAME}"
	echo "Environment file: ${ENV_FILE_NAME}"
	exit 1
fi
echo "Environment file found"

# Check if environment already exists and is valid
if [ -d "$ENV_PATH" ]; then
	echo "Environment $ENV_NAME already exists, updating..."
	echo "About to run: env update -f \"$ENV_YAML_PATH\" -p \"$ENV_PATH\" --yes"
	echo "File exists check: $(test -f "$ENV_YAML_PATH" && echo "YES" || echo "NO")"
	echo "File size: $(ls -la "$ENV_YAML_PATH" 2>/dev/null || echo "Cannot stat file")"

	if run_mamba env update -f "$ENV_YAML_PATH" -p "$ENV_PATH" -y; then
		echo "Successfully updated environment: $ENV_NAME"
	else
		echo "Failed to update environment: $ENV_NAME"
		exit 1
	fi
else
	echo "Environment $ENV_NAME does not exist, creating new environment..."
	echo "About to run: env create -f \"$ENV_YAML_PATH\" -p \"$ENV_PATH\" --yes"
	echo "File exists check: $(test -f "$ENV_YAML_PATH" && echo "YES" || echo "NO")"
	echo "File size: $(ls -la "$ENV_YAML_PATH" 2>/dev/null || echo "Cannot stat file")"

	if run_mamba env create -f "$ENV_YAML_PATH" -p "$ENV_PATH" -y; then
		echo "Successfully created environment: $ENV_NAME"
	else
		echo "Failed to create environment: $ENV_NAME"
		exit 1
	fi
fi

# Handle pip uninstall if specified
if [ -n "$PIP_UNINSTALL_FILE" ] && [ -f "${WORK_DIR}/${ENV_DIR_NAME}/${PIP_UNINSTALL_FILE}" ]; then
	echo "Uninstalling packages from ${PIP_UNINSTALL_FILE}"
	run_mamba run -p "$ENV_PATH" python -m pip uninstall -r "${WORK_DIR}/${ENV_DIR_NAME}/${PIP_UNINSTALL_FILE}" -y
fi

# Clean up
echo "Cleaning up..."
rm -rf "$WORK_DIR"

echo "Kernel building completed for environment: $ENV_NAME"
