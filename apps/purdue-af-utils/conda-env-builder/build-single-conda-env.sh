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

echo "Building conda environment: $ENV_NAME at $LOCATION_ROOT"

# Install required packages with mirror fallbacks
echo "Installing required packages..."
# Try multiple mirror sources to handle Rocky Linux mirror issues
for mirror in "https://mirrors.rockylinux.org" "https://mirror.rockylinux.org" "https://dl.rockylinux.org"; do
	if dnf install -y git wget bzip2 sudo python3-pip which --nogpgcheck --setopt=mirrorlist="${mirror}/mirrorlist?arch=x86_64&repo=baseos-8" 2>/dev/null; then
		echo "Successfully installed packages using mirror: $mirror"
		break
	else
		echo "Failed to install packages using mirror: $mirror, trying next..."
	fi
done

# Fallback: try without specific mirror if all mirrors fail
if ! rpm -q git wget bzip2 sudo python3-pip which >/dev/null 2>&1; then
	echo "All mirrors failed, trying default dnf configuration..."
	dnf install -y git wget bzip2 sudo python3-pip which --nogpgcheck || {
		echo "ERROR: Failed to install required packages even with fallbacks"
		exit 1
	}
fi

# Install ldap3 for LDAP lookups
pip3 install ldap3

# Ensure pip is available on PATH
if ! command -v pip >/dev/null 2>&1 && command -v pip3 >/dev/null 2>&1; then
	ln -sf "$(command -v pip3)" /usr/local/bin/pip
fi

# Install Miniconda using micromamba approach
echo "Installing Miniconda via micromamba..."
wget -qO /tmp/micromamba.tar.bz2 "https://micromamba.snakepit.net/api/micromamba/linux-64/latest"
tar -xvjf /tmp/micromamba.tar.bz2 --strip-components=1 bin/micromamba
chmod +x micromamba

# Install miniconda first, then mamba into it
./micromamba install --root-prefix="/opt/conda" --prefix="/opt/conda" --yes 'conda' 'pip' 'conda-env'
# Now install mamba into the conda environment (from conda-forge channel)
/opt/conda/bin/conda install -c conda-forge -y mamba
rm micromamba

# Create system-wide symlinks to make conda and mamba available everywhere
ln -sf /opt/conda/bin/conda /usr/local/bin/conda
ln -sf /opt/conda/bin/mamba /usr/local/bin/mamba
ln -sf /opt/conda/bin/python /usr/local/bin/python
ln -sf /opt/conda/bin/pip /usr/local/bin/pip

# Verify symlinks were created
ls -la /usr/local/bin/conda /usr/local/bin/mamba /usr/local/bin/python /usr/local/bin/pip >/dev/null 2>&1 || echo "Warning: Some symlinks failed to create"

# Verify conda installation
if ! /opt/conda/bin/conda --version >/dev/null 2>&1; then
	echo "ERROR: conda is not properly installed or not accessible"
	exit 1
fi
/opt/conda/bin/conda --version

# Verify mamba installation
if ! /opt/conda/bin/mamba --version >/dev/null 2>&1; then
	echo "ERROR: mamba is not properly installed or not accessible"
	exit 1
fi
/opt/conda/bin/mamba --version

# Ensure 'conda env' subcommand is available
if ! /opt/conda/bin/conda env --help >/dev/null 2>&1; then
	echo "'conda env' not available, installing conda-env..."
	/opt/conda/bin/conda install -y conda-env
fi

# Install ldap3 using the newly installed mamba's pip
echo "Installing ldap3..."
/opt/conda/bin/pip install ldap3

# LDAP lookup function
ldap_lookup() {
	local username="$1"
	/opt/conda/bin/python -c "
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

# Create user for running conda
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
USER_TMP="/tmp/conda-env-builder-${TARGET_USERNAME}-${ENV_NAME}-$$"
USER_CONDA="${USER_TMP}/conda"
USER_PKGS="${USER_TMP}/pkgs"
USER_ENVS="${USER_TMP}/envs"

# Clean up any existing directories to avoid permission issues
$RUN_AS_UID rm -rf "$USER_TMP" 2>/dev/null || true

# Create user-specific directories with proper ownership
$RUN_AS_UID mkdir -p "$USER_CONDA" "$USER_PKGS" "$USER_ENVS" "$USER_TMP/pip-cache" "$USER_TMP/work" "$USER_TMP/conda-tmp" "$USER_TMP/env"
# Create cache directory for HOME/XDG caches used by tooling
$RUN_AS_UID mkdir -p "$USER_TMP/.cache"
# Create conda-specific cache directories that conda expects
$RUN_AS_UID mkdir -p "$USER_TMP/.cache/conda/proc"
$RUN_AS_UID mkdir -p "$USER_TMP/.cache/conda/logs"
$RUN_AS_UID mkdir -p "$USER_TMP/.cache/conda/notices"

# Ensure full ownership of the temporary tree by the target user
chown -R "$TARGET_USERNAME:$TARGET_USERNAME" "$USER_TMP"
# Set proper permissions to ensure conda can write temporary files
chmod -R 755 "$USER_TMP"

# Debug: Show ownership and permissions of key directories
echo "Debug: Checking ownership and permissions of cache directories..."
ls -la "$USER_TMP/.cache/conda/" 2>/dev/null || echo "Cache directory not accessible"
ls -la "$USER_TMP/.cache/conda/notices" 2>/dev/null || echo "Notices directory not accessible"

# Verify the USER_TMP directory is writable
if [ ! -w "$USER_TMP" ]; then
	echo "ERROR: USER_TMP directory $USER_TMP is not writable!"
	exit 1
fi

# Verify the conda-tmp directory is writable
if [ ! -w "$USER_TMP/conda-tmp" ]; then
	echo "ERROR: Conda temp directory $USER_TMP/conda-tmp is not writable!"
	exit 1
fi

# Let conda/mamba manage internal subdirectories - no need to create them manually
chown -R "$TARGET_USERNAME:$TARGET_USERNAME" "$USER_PKGS" "$USER_ENVS"
chmod -R 755 "$USER_PKGS" "$USER_ENVS"

# Clean up any stale lockfiles and temporary files
$RUN_AS_UID find "$USER_TMP" -name "*.lock" -delete 2>/dev/null || true
$RUN_AS_UID find "$USER_TMP" -name "conda*" -delete 2>/dev/null || true

# Create conda configuration file
$RUN_AS_UID bash -c "cat >'${USER_TMP}/.condarc' <<'EOF'
pkgs_dirs:
  - $USER_PKGS
envs_dirs:
  - $USER_ENVS
channels:
  - conda-forge
  - defaults
# Use strict channel priority and current repodata for stable solves and no-op optimization
channel_priority: strict
repodata_fns: [current_repodata.json]
use_lockfiles: false
# Disable aggressive caching
aggressive_update_packages: []
# Avoid hardlinks on network filesystems - keep copy mode for reliability
always_copy: true
# Disable conda activation hooks to prevent PATH conflicts
auto_activate_base: false
env_prompt: '({name})'
EOF"

# Verify conda is working
$RUN_AS_UID bash -c "
export CONDA_PKGS_DIRS='$USER_PKGS'
export CONDA_ENVS_PATH='$USER_ENVS'
export CONDA_CONFIG_FILE='${USER_TMP}/.condarc'
export PIP_CACHE_DIR='$USER_TMP/pip-cache'
export TMPDIR='$USER_TMP/conda-tmp'
export TEMP='$USER_TMP/conda-tmp'
export TMP='$USER_TMP/conda-tmp'
export CONDA_ALWAYS_COPY='1'
export PATH='/opt/conda/bin:/usr/local/bin:/usr/bin:/bin'
# Use direct conda execution to avoid initialization issues
/opt/conda/bin/conda --version
"

# Test conda env subcommand specifically
$RUN_AS_UID bash -c "
export CONDA_PKGS_DIRS='$USER_PKGS'
export CONDA_ENVS_PATH='$USER_ENVS'
export CONDA_CONFIG_FILE='${USER_TMP}/.condarc'
export PIP_CACHE_DIR='$USER_TMP/pip-cache'
export TMPDIR='$USER_TMP/conda-tmp'
export TEMP='$USER_TMP/conda-tmp'
export TMP='$USER_TMP/conda-tmp'
export CONDA_ALWAYS_COPY='1'
export PATH='/opt/conda/bin:/usr/local/bin:/usr/bin:/bin'
# Use direct conda execution to avoid initialization issues
/opt/conda/bin/conda env --help
"

# Set up paths - ensure no double slashes
ENV_PATH="${LOCATION_ROOT%/}/$ENV_NAME"
ENV_DIR_NAME="$ENV_DIR"
ENV_FILE_NAME="$ENV_FILE"
ENV_YAML_PATH=""

echo "Building environment '$ENV_NAME' at '$ENV_PATH'"

# Ensure the target environment directory exists (ownership handled by init container)
PARENT_DIR=$(dirname "$ENV_PATH")

if [ ! -d "$PARENT_DIR" ]; then
	echo "Creating parent directory: $PARENT_DIR"
	$RUN_AS_UID mkdir -p "$PARENT_DIR"
fi

if [ ! -d "$ENV_PATH" ]; then
	$RUN_AS_UID mkdir -p "$ENV_PATH"
fi

# Function to run conda with fallback pip handling
run_conda() {
	# Capture the output and error to examine what happened
	local output_file="$USER_TMP/conda_output.log"
	local error_file="$USER_TMP/conda_error.log"

	if sudo -E -u "$TARGET_USERNAME" bash -c "
cd '$USER_TMP'
export PWD='$USER_TMP'
export SHELL='/bin/bash'
# Set a clean PATH to avoid conflicts with existing conda installations
export PATH='/opt/conda/bin:/usr/local/bin:/usr/bin:/bin'
export CONDA_ROOT_PREFIX='$USER_CONDA'
export CONDA_PKGS_DIRS='$USER_PKGS'
export CONDA_ENVS_PATH='$USER_ENVS'
export CONDA_CONFIG_FILE='${USER_TMP}/.condarc'
export PIP_CACHE_DIR='$USER_TMP/pip-cache'
export TMPDIR='$USER_TMP/conda-tmp'
export TEMP='$USER_TMP/conda-tmp'
export TMP='$USER_TMP/conda-tmp'
export OLDPWD='$USER_TMP'
export HOME='$USER_TMP'
export XDG_CACHE_HOME='$USER_TMP/.cache'
export CONDA_ALWAYS_COPY='1'
export CONDA_ALWAYS_YES='true'
# Point to the target environment's pip explicitly
export _CE_CONDA_PIP_EXE='$ENV_PATH/bin/pip'
# Use direct conda execution to avoid initialization issues

/opt/conda/bin/conda \"\$@\"
" "$@" >"$output_file" 2>"$error_file"; then
		return 0
	else
		local exit_code=$?
		echo "Conda failed with exit code: $exit_code"
		echo "=== ERROR OUTPUT ==="
		cat "$error_file"
		echo "=== STANDARD OUTPUT ==="
		cat "$output_file"
		return $exit_code
	fi
}

# Helper: check if environment needs changes using dry-run
env_needs_change() {
	local prefix="$1"
	local yaml_path="$2"
	local output_file="$USER_TMP/dry_run_output.log"
	local error_file="$USER_TMP/dry_run_error.log"

	# Try mamba first with --dry-run --offline
	if sudo -E -u "$TARGET_USERNAME" bash -c "
cd '$USER_TMP'
export PWD='$USER_TMP'
export SHELL='/bin/bash'
export PATH='/opt/conda/bin:/usr/local/bin:/usr/bin:/bin'
export CONDA_PKGS_DIRS='$USER_PKGS'
export CONDA_ENVS_PATH='$USER_ENVS'
export CONDA_CONFIG_FILE='${USER_TMP}/.condarc'
export PIP_CACHE_DIR='$USER_TMP/pip-cache'
export TMPDIR='$USER_TMP/conda-tmp'
export TEMP='$USER_TMP/conda-tmp'
export TMP='$USER_TMP/conda-tmp'
export HOME='$USER_TMP'
export XDG_CACHE_HOME='$USER_TMP/.cache'
export CONDA_ALWAYS_COPY='1'
export CONDA_ALWAYS_YES='true'
/opt/conda/bin/mamba env update --dry-run --offline --prefix '$prefix' --file '$yaml_path'
" >"$output_file" 2>"$error_file"; then
		# Check if output indicates no changes needed
		if grep -q -E "(All requested packages already installed|Transaction will be empty|Nothing to do)" "$output_file"; then
			return 1 # No changes needed
		fi
		return 0 # Changes needed
	else
		# Mamba failed, try conda with same flags
		if sudo -E -u "$TARGET_USERNAME" bash -c "
cd '$USER_TMP'
export PWD='$USER_TMP'
export SHELL='/bin/bash'
export PATH='/opt/conda/bin:/usr/local/bin:/usr/bin:/bin'
export CONDA_PKGS_DIRS='$USER_PKGS'
export CONDA_ENVS_PATH='$USER_ENVS'
export CONDA_CONFIG_FILE='${USER_TMP}/.condarc'
export PIP_CACHE_DIR='$USER_TMP/pip-cache'
export TMPDIR='$USER_TMP/conda-tmp'
export TEMP='$USER_TMP/conda-tmp'
export TMP='$USER_TMP/conda-tmp'
export HOME='$USER_TMP'
export XDG_CACHE_HOME='$USER_TMP/.cache'
export CONDA_ALWAYS_COPY='1'
export CONDA_ALWAYS_YES='true'
/opt/conda/bin/conda env update --dry-run --offline --prefix '$prefix' --file '$yaml_path'
" >"$output_file" 2>"$error_file"; then
			# Check if output indicates no changes needed
			if grep -q -E "(All requested packages already installed|Transaction will be empty|Nothing to do)" "$output_file"; then
				return 1 # No changes needed
			fi
			return 0 # Changes needed
		else
			# Both failed, assume changes needed
			return 0
		fi
	fi
}

# Helper: detect filesystem types for ENV and PKGS directories
detect_filesystem_types() {
	local env_path="$1"
	local pkgs_path="$2"

	# Get filesystem type for environment path (or its parent if env doesn't exist)
	local env_check_path="$env_path"
	if [ ! -d "$env_path" ]; then
		env_check_path="$(dirname "$env_path")"
	fi

	# Detect filesystem types
	ENV_FS=$(stat -f -c %T "$env_check_path" 2>/dev/null || echo "unknown")
	PKGS_FS=$(stat -f -c %T "$pkgs_path" 2>/dev/null || echo "unknown")

	echo "Filesystem detection: ENV_FS=$ENV_FS, PKGS_FS=$PKGS_FS"

	# Check if we need relocation (different FS or network/overlay FS)
	if [ "$ENV_FS" != "$PKGS_FS" ] || [[ "$ENV_FS" =~ (nfs|smb|cifs|fuseblk|overlay) ]]; then
		return 0 # Need relocation
	else
		return 1 # No relocation needed
	fi
}

# Helper: update/create environment using local build + conda-pack relocation for network FS
update_env_via_relocation() {
	local env_path="$1"
	local yaml_path="$2"
	local env_name="$3"

	echo "Using local build + relocation for network/overlay filesystem..."

	# Ensure conda-pack is installed
	echo "Installing conda-pack..."
	if ! $RUN_AS_UID bash -c "
cd '$USER_TMP'
export PATH='/opt/conda/bin:/usr/local/bin:/usr/bin:/bin'
export CONDA_PKGS_DIRS='$USER_PKGS'
export CONDA_ENVS_PATH='$USER_ENVS'
export CONDA_CONFIG_FILE='${USER_TMP}/.condarc'
export PIP_CACHE_DIR='$USER_TMP/pip-cache'
export TMPDIR='$USER_TMP/conda-tmp'
export TEMP='$USER_TMP/conda-tmp'
export TMP='$USER_TMP/conda-tmp'
export HOME='$USER_TMP'
export XDG_CACHE_HOME='$USER_TMP/.cache'
export CONDA_ALWAYS_COPY='1'
export CONDA_ALWAYS_YES='true'
/opt/conda/bin/conda install -y -c conda-forge conda-pack
" >"$USER_TMP/conda_pack_install.log" 2>"$USER_TMP/conda_pack_error.log"; then
		echo "Failed to install conda-pack, falling back to direct update"
		return 1
	fi

	# Set up local build directory
	local LOCAL_BUILD_PREFIX="$USER_TMP/envbuild-$env_name"
	local PACK_TGZ="$USER_TMP/$env_name.tar.gz"

	# Clean up any existing build
	$RUN_AS_UID rm -rf "$LOCAL_BUILD_PREFIX" "$PACK_TGZ" 2>/dev/null || true

	# Create/update environment at local build prefix
	echo "Building environment locally at $LOCAL_BUILD_PREFIX..."
	if update_env_via_activation "$LOCAL_BUILD_PREFIX" "$yaml_path"; then
		echo "Local build succeeded"
	else
		echo "Local build failed, trying create..."
		if create_env_via_activation "$LOCAL_BUILD_PREFIX" "$yaml_path"; then
			echo "Local create succeeded"
		else
			echo "Local build/create failed"
			return 1
		fi
	fi

	# Pack the environment
	echo "Packing environment..."
	if $RUN_AS_UID bash -c "
cd '$USER_TMP'
export PATH='$LOCAL_BUILD_PREFIX/bin:/opt/conda/bin:/usr/local/bin:/usr/bin:/bin'
export CONDA_PKGS_DIRS='$USER_PKGS'
export CONDA_ENVS_PATH='$USER_ENVS'
export CONDA_CONFIG_FILE='${USER_TMP}/.condarc'
export PIP_CACHE_DIR='$USER_TMP/pip-cache'
export TMPDIR='$USER_TMP/conda-tmp'
export TEMP='$USER_TMP/conda-tmp'
export TMP='$USER_TMP/conda-tmp'
export HOME='$USER_TMP'
export XDG_CACHE_HOME='$USER_TMP/.cache'
export CONDA_ALWAYS_COPY='1'
export CONDA_ALWAYS_YES='true'
/opt/conda/bin/conda-pack -p '$LOCAL_BUILD_PREFIX' -o '$PACK_TGZ'
" >"$USER_TMP/conda_pack_output.log" 2>"$USER_TMP/conda_pack_error.log"; then
		echo "Environment packed successfully"
	else
		echo "Failed to pack environment"
		cat "$USER_TMP/conda_pack_error.log"
		return 1
	fi

	# Extract to target location
	echo "Extracting to target location $env_path..."
	$RUN_AS_UID rm -rf "$env_path" 2>/dev/null || true
	$RUN_AS_UID mkdir -p "$env_path"

	if $RUN_AS_UID bash -c "cd '$env_path' && tar -xzf '$PACK_TGZ'"; then
		echo "Extraction succeeded"
	else
		echo "Failed to extract environment"
		return 1
	fi

	# Run conda-unpack to fix paths
	echo "Running conda-unpack..."
	if $RUN_AS_UID bash -c "cd '$env_path' && ./bin/conda-unpack"; then
		echo "conda-unpack succeeded"
	else
		echo "conda-unpack failed, but environment may still work"
	fi

	# Clean up
	$RUN_AS_UID rm -rf "$LOCAL_BUILD_PREFIX" "$PACK_TGZ" 2>/dev/null || true

	echo "Relocation completed successfully"
	return 0
}

# Helper: update environment using --prefix to avoid activation issues
update_env_via_activation() {
	local env_path="$1"
	local yaml_path="$2"
	local output_file="$USER_TMP/conda_update_output.log"
	local error_file="$USER_TMP/conda_update_error.log"

	# Try mamba first with --offline, then fall back to online if needed
	if sudo -E -u "$TARGET_USERNAME" bash -c "
cd '$USER_TMP'
export PWD='$USER_TMP'
export SHELL='/bin/bash'
export PATH='/opt/conda/bin:/usr/local/bin:/usr/bin:/bin'
export CONDA_PKGS_DIRS='$USER_PKGS'
export CONDA_ENVS_PATH='$USER_ENVS'
export CONDA_CONFIG_FILE='${USER_TMP}/.condarc'
export PIP_CACHE_DIR='$USER_TMP/pip-cache'
export TMPDIR='$USER_TMP/conda-tmp'
export TEMP='$USER_TMP/conda-tmp'
export TMP='$USER_TMP/conda-tmp'
export HOME='$USER_TMP'
export XDG_CACHE_HOME='$USER_TMP/.cache'
export CONDA_ALWAYS_COPY='1'
export CONDA_ALWAYS_YES='true'
# Use --prefix instead of activation to avoid PATH conflicts
/opt/conda/bin/mamba env update --offline --file '$yaml_path' --prefix '$env_path'
" >"$output_file" 2>"$error_file"; then
		return 0
	else
		local exit_code=$?
		echo "Mamba offline update failed with exit code: $exit_code, trying mamba online..."
		echo "=== MAMBA OFFLINE ERROR OUTPUT ==="
		cat "$error_file"
		echo "=== MAMBA OFFLINE STANDARD OUTPUT ==="
		cat "$output_file"

		# Try mamba online (remove --offline)
		if sudo -E -u "$TARGET_USERNAME" bash -c "
cd '$USER_TMP'
export PWD='$USER_TMP'
export SHELL='/bin/bash'
export PATH='/opt/conda/bin:/usr/local/bin:/usr/bin:/bin'
export CONDA_PKGS_DIRS='$USER_PKGS'
export CONDA_ENVS_PATH='$USER_ENVS'
export CONDA_CONFIG_FILE='${USER_TMP}/.condarc'
export PIP_CACHE_DIR='$USER_TMP/pip-cache'
export TMPDIR='$USER_TMP/conda-tmp'
export TEMP='$USER_TMP/conda-tmp'
export TMP='$USER_TMP/conda-tmp'
export HOME='$USER_TMP'
export XDG_CACHE_HOME='$USER_TMP/.cache'
export CONDA_ALWAYS_COPY='1'
export CONDA_ALWAYS_YES='true'
# Use --prefix instead of activation to avoid PATH conflicts
/opt/conda/bin/mamba env update --file '$yaml_path' --prefix '$env_path'
" >"$output_file" 2>"$error_file"; then
			echo "Mamba online update succeeded"
			return 0
		else
			local mamba_online_exit_code=$?
			echo "Mamba online update failed with exit code: $mamba_online_exit_code, trying conda fallback..."
			echo "=== MAMBA ONLINE ERROR OUTPUT ==="
			cat "$error_file"
			echo "=== MAMBA ONLINE STANDARD OUTPUT ==="
			cat "$output_file"

			# Fallback to conda env update (offline first, then online)
			if sudo -E -u "$TARGET_USERNAME" bash -c "
cd '$USER_TMP'
export PWD='$USER_TMP'
export SHELL='/bin/bash'
export PATH='/opt/conda/bin:/usr/local/bin:/usr/bin:/bin'
export CONDA_PKGS_DIRS='$USER_PKGS'
export CONDA_ENVS_PATH='$USER_ENVS'
export CONDA_CONFIG_FILE='${USER_TMP}/.condarc'
export PIP_CACHE_DIR='$USER_TMP/pip-cache'
export TMPDIR='$USER_TMP/conda-tmp'
export TEMP='$USER_TMP/conda-tmp'
export TMP='$USER_TMP/conda-tmp'
export HOME='$USER_TMP'
export XDG_CACHE_HOME='$USER_TMP/.cache'
export CONDA_ALWAYS_COPY='1'
export CONDA_ALWAYS_YES='true'
# Use --prefix instead of activation to avoid PATH conflicts
/opt/conda/bin/conda env update --offline --file '$yaml_path' --prefix '$env_path'
" >"$output_file" 2>"$error_file"; then
				echo "Conda offline fallback succeeded"
				return 0
			else
				# Try conda online
				if sudo -E -u "$TARGET_USERNAME" bash -c "
cd '$USER_TMP'
export PWD='$USER_TMP'
export SHELL='/bin/bash'
export PATH='/opt/conda/bin:/usr/local/bin:/usr/bin:/bin'
export CONDA_PKGS_DIRS='$USER_PKGS'
export CONDA_ENVS_PATH='$USER_ENVS'
export CONDA_CONFIG_FILE='${USER_TMP}/.condarc'
export PIP_CACHE_DIR='$USER_TMP/pip-cache'
export TMPDIR='$USER_TMP/conda-tmp'
export TEMP='$USER_TMP/conda-tmp'
export TMP='$USER_TMP/conda-tmp'
export HOME='$USER_TMP'
export XDG_CACHE_HOME='$USER_TMP/.cache'
export CONDA_ALWAYS_COPY='1'
export CONDA_ALWAYS_YES='true'
# Use --prefix instead of activation to avoid PATH conflicts
/opt/conda/bin/conda env update --file '$yaml_path' --prefix '$env_path'
" >"$output_file" 2>"$error_file"; then
					echo "Conda online fallback succeeded"
					return 0
				else
					local conda_exit_code=$?
					echo "All conda/mamba attempts failed with exit code: $conda_exit_code"
					echo "=== CONDA ERROR OUTPUT ==="
					cat "$error_file"
					echo "=== CONDA STANDARD OUTPUT ==="
					cat "$output_file"
					return $conda_exit_code
				fi
			fi
		fi
	fi
}

# Helper: create environment using --prefix to avoid activation issues
create_env_via_activation() {
	local env_path="$1"
	local yaml_path="$2"
	local output_file="$USER_TMP/conda_create_output.log"
	local error_file="$USER_TMP/conda_create_error.log"

	# Try mamba first with --offline, then fall back to online if needed
	if sudo -E -u "$TARGET_USERNAME" bash -c "
cd '$USER_TMP'
export PWD='$USER_TMP'
export SHELL='/bin/bash'
export PATH='/opt/conda/bin:/usr/local/bin:/usr/bin:/bin'
export CONDA_PKGS_DIRS='$USER_PKGS'
export CONDA_ENVS_PATH='$USER_ENVS'
export CONDA_CONFIG_FILE='${USER_TMP}/.condarc'
export PIP_CACHE_DIR='$USER_TMP/pip-cache'
export TMPDIR='$USER_TMP/conda-tmp'
export TEMP='$USER_TMP/conda-tmp'
export TMP='$USER_TMP/conda-tmp'
export HOME='$USER_TMP'
export XDG_CACHE_HOME='$USER_TMP/.cache'
export CONDA_ALWAYS_COPY='1'
export CONDA_ALWAYS_YES='true'
# Use --prefix instead of activation to avoid PATH conflicts
/opt/conda/bin/mamba env create --offline --file '$yaml_path' --prefix '$env_path'
" >"$output_file" 2>"$error_file"; then
		return 0
	else
		local exit_code=$?
		echo "Mamba offline create failed with exit code: $exit_code, trying mamba online..."
		echo "=== MAMBA OFFLINE ERROR OUTPUT ==="
		cat "$error_file"
		echo "=== MAMBA OFFLINE STANDARD OUTPUT ==="
		cat "$output_file"

		# Try mamba online (remove --offline)
		if sudo -E -u "$TARGET_USERNAME" bash -c "
cd '$USER_TMP'
export PWD='$USER_TMP'
export SHELL='/bin/bash'
export PATH='/opt/conda/bin:/usr/local/bin:/usr/bin:/bin'
export CONDA_PKGS_DIRS='$USER_PKGS'
export CONDA_ENVS_PATH='$USER_ENVS'
export CONDA_CONFIG_FILE='${USER_TMP}/.condarc'
export PIP_CACHE_DIR='$USER_TMP/pip-cache'
export TMPDIR='$USER_TMP/conda-tmp'
export TEMP='$USER_TMP/conda-tmp'
export TMP='$USER_TMP/conda-tmp'
export HOME='$USER_TMP'
export XDG_CACHE_HOME='$USER_TMP/.cache'
export CONDA_ALWAYS_COPY='1'
export CONDA_ALWAYS_YES='true'
# Use --prefix instead of activation to avoid PATH conflicts
/opt/conda/bin/mamba env create --file '$yaml_path' --prefix '$env_path'
" >"$output_file" 2>"$error_file"; then
			echo "Mamba online create succeeded"
			return 0
		else
			local mamba_online_exit_code=$?
			echo "Mamba online create failed with exit code: $mamba_online_exit_code, trying conda fallback..."
			echo "=== MAMBA ONLINE ERROR OUTPUT ==="
			cat "$error_file"
			echo "=== MAMBA ONLINE STANDARD OUTPUT ==="
			cat "$output_file"

			# Fallback to conda env create (offline first, then online)
			if sudo -E -u "$TARGET_USERNAME" bash -c "
cd '$USER_TMP'
export PWD='$USER_TMP'
export SHELL='/bin/bash'
export PATH='/opt/conda/bin:/usr/local/bin:/usr/bin:/bin'
export CONDA_PKGS_DIRS='$USER_PKGS'
export CONDA_ENVS_PATH='$USER_ENVS'
export CONDA_CONFIG_FILE='${USER_TMP}/.condarc'
export PIP_CACHE_DIR='$USER_TMP/pip-cache'
export TMPDIR='$USER_TMP/conda-tmp'
export TEMP='$USER_TMP/conda-tmp'
export TMP='$USER_TMP/conda-tmp'
export HOME='$USER_TMP'
export XDG_CACHE_HOME='$USER_TMP/.cache'
export CONDA_ALWAYS_COPY='1'
export CONDA_ALWAYS_YES='true'
# Use --prefix instead of activation to avoid PATH conflicts
/opt/conda/bin/conda env create --offline --file '$yaml_path' --prefix '$env_path'
" >"$output_file" 2>"$error_file"; then
				echo "Conda offline fallback succeeded"
				return 0
			else
				# Try conda online
				if sudo -E -u "$TARGET_USERNAME" bash -c "
cd '$USER_TMP'
export PWD='$USER_TMP'
export SHELL='/bin/bash'
export PATH='/opt/conda/bin:/usr/local/bin:/usr/bin:/bin'
export CONDA_PKGS_DIRS='$USER_PKGS'
export CONDA_ENVS_PATH='$USER_ENVS'
export CONDA_CONFIG_FILE='${USER_TMP}/.condarc'
export PIP_CACHE_DIR='$USER_TMP/pip-cache'
export TMPDIR='$USER_TMP/conda-tmp'
export TEMP='$USER_TMP/conda-tmp'
export TMP='$USER_TMP/conda-tmp'
export HOME='$USER_TMP'
export XDG_CACHE_HOME='$USER_TMP/.cache'
export CONDA_ALWAYS_COPY='1'
export CONDA_ALWAYS_YES='true'
# Use --prefix instead of activation to avoid PATH conflicts
/opt/conda/bin/conda env create --file '$yaml_path' --prefix '$env_path'
" >"$output_file" 2>"$error_file"; then
					echo "Conda online fallback succeeded"
					return 0
				else
					local conda_exit_code=$?
					echo "All conda/mamba attempts failed with exit code: $conda_exit_code"
					echo "=== CONDA ERROR OUTPUT ==="
					cat "$error_file"
					echo "=== CONDA STANDARD OUTPUT ==="
					cat "$output_file"
					return $conda_exit_code
				fi
			fi
		fi
	fi
}

# Function to run commands within a conda environment
run_in_env() {
	local env_path="$1"
	shift
	echo "Running command in environment: $env_path"
	echo "Command: $*"

	# Build the command string properly
	local cmd_string="cd '$USER_TMP' && "
	cmd_string+="export PWD='$USER_TMP' && "
	cmd_string+="export SHELL='/bin/bash' && "
	cmd_string+="export PATH='$env_path/bin:/opt/conda/bin:/usr/local/bin:/usr/bin:/bin' && "
	cmd_string+="export CONDA_PKGS_DIRS='$USER_PKGS' && "
	cmd_string+="export CONDA_ENVS_PATH='$USER_ENVS' && "
	cmd_string+="export CONDA_CONFIG_FILE='${USER_TMP}/.condarc' && "
	cmd_string+="export PIP_CACHE_DIR='$USER_TMP/pip-cache' && "
	cmd_string+="export TMPDIR='$USER_TMP/conda-tmp' && "
	cmd_string+="export TEMP='$USER_TMP/conda-tmp' && "
	cmd_string+="export TMP='$USER_TMP/conda-tmp' && "
	cmd_string+="export HOME='$USER_TMP' && "
	cmd_string+="export XDG_CACHE_HOME='$USER_TMP/.cache' && "
	cmd_string+="export CONDA_ALWAYS_COPY='1' && "
	cmd_string+="export _CE_CONDA_PIP_EXE='$env_path/bin/pip' && "
	cmd_string+="export CONDA_PREFIX='$env_path' && "
	cmd_string+="export CONDA_DEFAULT_ENV='$(basename "$env_path")' && "
	cmd_string+="$*"

	if sudo -E -u "$TARGET_USERNAME" bash -c "$cmd_string"; then
		return 0
	else
		return $?
	fi
}

# Use /tmp for build directory - no permission issues, use timestamp and PID for uniqueness
BUILD_DIR="/tmp/conda-env-builds-$(date +%s)-$$"

# Clean up old temporary directories from previous runs
$RUN_AS_UID find /tmp -maxdepth 1 -name "conda-env-builder-${TARGET_USERNAME}-*" -type d -mmin +10 -exec rm -rf {} \; 2>/dev/null || true
$RUN_AS_UID find /tmp -maxdepth 1 -name "conda-env-builds-*" -type d -mmin +10 -exec rm -rf {} \; 2>/dev/null || true

# Create build directory as target user
$RUN_AS_UID mkdir -p "$BUILD_DIR" || true
chown -R "$TARGET_USERNAME:$TARGET_USERNAME" "$BUILD_DIR"
chmod -R 755 "$BUILD_DIR"

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

# Copy environment file to our controlled directory to avoid working directory issues
ENV_YAML_COPY="${USER_TMP}/env/environment.yaml"
$RUN_AS_UID mkdir -p "$(dirname "$ENV_YAML_COPY")"
$RUN_AS_UID cp "$ENV_YAML_PATH" "$ENV_YAML_COPY"

# Normalize line endings and validate YAML format
echo "Validating and normalizing environment.yaml file..."
$RUN_AS_UID bash -c "
# Remove carriage returns and normalize line endings
sed -i 's/\r$//' '$ENV_YAML_COPY'
# Remove any trailing whitespace
sed -i 's/[[:space:]]*$//' '$ENV_YAML_COPY'
# Ensure file ends with newline
sed -i -e :a -e '/^\n*$/{$d;N;ba' -e '}' '$ENV_YAML_COPY'
"

# Basic YAML validation using grep and basic syntax checks
echo "Performing basic YAML validation..."
if ! $RUN_AS_UID bash -c "
# Check for basic YAML structure
if grep -q '^name:' '$ENV_YAML_COPY' && grep -q '^channels:' '$ENV_YAML_COPY' && grep -q '^dependencies:' '$ENV_YAML_COPY'; then
    echo 'Basic YAML structure validation passed'
    exit 0
else
    echo 'Basic YAML structure validation failed - missing required sections'
    exit 1
fi
"; then
	echo "ERROR: YAML structure validation failed!"
	echo "=== YAML FILE CONTENTS ==="
	cat "$ENV_YAML_COPY"
	echo "=== END YAML CONTENTS ==="
	exit 1
fi

echo "✓ YAML file validated successfully"

# Check if environment already exists and is valid
if [ -d "$ENV_PATH" ]; then
	# Verify it's actually a valid conda environment by checking for conda-meta
	if [ -d "$ENV_PATH/conda-meta" ] && [ -f "$ENV_PATH/conda-meta/history" ]; then
		echo "Checking if environment needs updates: $ENV_NAME"

		# Fast no-op detection
		if ! env_needs_change "$ENV_PATH" "$ENV_YAML_COPY"; then
			echo "No changes. Skipping update."
		else
			echo "Changes required. Updating environment: $ENV_NAME"

			# Check if we need relocation for network/overlay filesystems
			if detect_filesystem_types "$ENV_PATH" "$USER_PKGS"; then
				echo "Using relocation path for network/overlay filesystem"
				if update_env_via_relocation "$ENV_PATH" "$ENV_YAML_COPY" "$ENV_NAME"; then
					echo "✓ Environment updated successfully via relocation"
				else
					echo "ERROR: Failed to update environment via relocation, trying direct update..."
					if update_env_via_activation "$ENV_PATH" "$ENV_YAML_COPY"; then
						echo "✓ Environment updated successfully via direct update"
					else
						echo "ERROR: Failed to update environment, recreating..."
						rm -rf "$ENV_PATH"
						if create_env_via_activation "$ENV_PATH" "$ENV_YAML_COPY"; then
							echo "✓ Environment recreated successfully"
						else
							echo "ERROR: Failed to recreate environment: $ENV_NAME"
							exit 1
						fi
					fi
				fi
			else
				echo "Using direct update for local filesystem"
				if update_env_via_activation "$ENV_PATH" "$ENV_YAML_COPY"; then
					echo "✓ Environment updated successfully"
				else
					echo "ERROR: Failed to update environment, recreating..."
					rm -rf "$ENV_PATH"
					if create_env_via_activation "$ENV_PATH" "$ENV_YAML_COPY"; then
						echo "✓ Environment recreated successfully"
					else
						echo "ERROR: Failed to recreate environment: $ENV_NAME"
						exit 1
					fi
				fi
			fi
		fi
	else
		echo "Recreating invalid environment: $ENV_NAME"
		rm -rf "$ENV_PATH"

		# Check if we need relocation for network/overlay filesystems
		if detect_filesystem_types "$ENV_PATH" "$USER_PKGS"; then
			echo "Using relocation path for network/overlay filesystem"
			if update_env_via_relocation "$ENV_PATH" "$ENV_YAML_COPY" "$ENV_NAME"; then
				echo "✓ Environment created successfully via relocation"
			else
				echo "ERROR: Failed to create environment via relocation, trying direct create..."
				if create_env_via_activation "$ENV_PATH" "$ENV_YAML_COPY"; then
					echo "✓ Environment created successfully via direct create"
				else
					echo "ERROR: Failed to create environment: $ENV_NAME"
					exit 1
				fi
			fi
		else
			echo "Using direct create for local filesystem"
			if create_env_via_activation "$ENV_PATH" "$ENV_YAML_COPY"; then
				echo "✓ Environment created successfully"
			else
				echo "ERROR: Failed to create environment: $ENV_NAME"
				exit 1
			fi
		fi
	fi
else
	echo "Creating new environment: $ENV_NAME"

	# Check if we need relocation for network/overlay filesystems
	if detect_filesystem_types "$ENV_PATH" "$USER_PKGS"; then
		echo "Using relocation path for network/overlay filesystem"
		if update_env_via_relocation "$ENV_PATH" "$ENV_YAML_COPY" "$ENV_NAME"; then
			echo "✓ Environment created successfully via relocation"
		else
			echo "ERROR: Failed to create environment via relocation, trying direct create..."
			if create_env_via_activation "$ENV_PATH" "$ENV_YAML_COPY"; then
				echo "✓ Environment created successfully via direct create"
			else
				echo "ERROR: Failed to create environment: $ENV_NAME"
				exit 1
			fi
		fi
	else
		echo "Using direct create for local filesystem"
		if create_env_via_activation "$ENV_PATH" "$ENV_YAML_COPY"; then
			echo "✓ Environment created successfully"
		else
			echo "ERROR: Failed to create environment: $ENV_NAME"
			exit 1
		fi
	fi
fi

# Handle pip uninstall if specified
if [ -n "$PIP_UNINSTALL_FILE" ]; then
	if [ -f "${WORK_DIR}/${ENV_DIR_NAME}/${PIP_UNINSTALL_FILE}" ]; then
		echo "Uninstalling packages from ${PIP_UNINSTALL_FILE}"

		# Read the uninstall file and process each package individually
		while IFS= read -r package || [ -n "$package" ]; do
			# Skip empty lines and comments
			package=$(echo "$package" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
			if [[ -n "$package" && ! "$package" =~ ^[[:space:]]*# ]]; then
				# Check if package is actually installed
				if run_in_env "$ENV_PATH" python -m pip show "$package" >/dev/null 2>&1; then
					echo "Uninstalling $package..."
					if run_in_env "$ENV_PATH" python -m pip uninstall "$package" -y; then
						echo "✓ Uninstalled $package"
					else
						echo "⚠ Failed to uninstall $package"
					fi
				else
					echo "Skipping $package (not installed)"
				fi
			fi
		done <"${WORK_DIR}/${ENV_DIR_NAME}/${PIP_UNINSTALL_FILE}"

		echo "Pip uninstall completed"
	fi
fi

# Clean up
$RUN_AS_UID rm -rf "$WORK_DIR"

# Clean up any temporary files that might have been created
$RUN_AS_UID find "$BUILD_DIR" -name "*.tmp" -delete 2>/dev/null || true
$RUN_AS_UID find "$BUILD_DIR" -name "*.lock" -delete 2>/dev/null || true
$RUN_AS_UID find "$BUILD_DIR" -name "*.cache" -delete 2>/dev/null || true

# Final cleanup of temporary directories
$RUN_AS_UID find /tmp -maxdepth 1 -name "conda-env-builder-${TARGET_USERNAME}-*" -type d -mmin +5 -exec rm -rf {} \; 2>/dev/null || true
$RUN_AS_UID find /tmp -maxdepth 1 -name "conda-env-builds-*" -type d -mmin +5 -exec rm -rf {} \; 2>/dev/null || true

# Final verification of the built environment
if [ -d "$ENV_PATH" ] && [ -d "$ENV_PATH/conda-meta" ]; then
	# Check if python is available and working
	if [ -f "$ENV_PATH/bin/python" ]; then
		if $RUN_AS_UID bash -c "export PATH='$ENV_PATH/bin:/opt/conda/bin:/usr/local/bin:/usr/bin:/bin' && '$ENV_PATH/bin/python' --version" >/dev/null 2>&1; then
			echo "✓ Environment verification passed"
		else
			echo "⚠ Environment verification failed - Python not working"
		fi
	else
		echo "⚠ Environment verification failed - Python not found"
	fi
else
	echo "⚠ Environment verification failed - missing required directories"
fi

echo "✓ Environment build completed: $ENV_NAME"
