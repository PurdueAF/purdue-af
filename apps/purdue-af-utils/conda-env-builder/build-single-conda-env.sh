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

# Install Miniconda using micromamba approach (bootstrap only, do not use mamba later)
echo "Installing Miniconda via micromamba..."
wget -qO /tmp/micromamba.tar.bz2 "https://micromamba.snakepit.net/api/micromamba/linux-64/latest"
tar -xvjf /tmp/micromamba.tar.bz2 --strip-components=1 bin/micromamba
chmod +x micromamba
./micromamba install --root-prefix="/opt/conda" --prefix="/opt/conda" --yes 'conda' 'pip' 'conda-env'
rm micromamba

# Create system-wide symlinks to make conda available everywhere
ln -sf /opt/conda/bin/conda /usr/local/bin/conda
ln -sf /opt/conda/bin/python /usr/local/bin/python
ln -sf /opt/conda/bin/pip /usr/local/bin/pip

# Verify symlinks were created
ls -la /usr/local/bin/conda /usr/local/bin/python /usr/local/bin/pip >/dev/null 2>&1 || echo "Warning: Some symlinks failed to create"

# Verify conda installation
/opt/conda/bin/conda --version

# Ensure 'conda env' subcommand is available
if ! /opt/conda/bin/conda env --help >/dev/null 2>&1; then
	echo "'conda env' not available, installing conda-env..."
	/opt/conda/bin/conda install -y conda-env
fi

# Install ldap3 using conda's pip
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
	TARGET_UID=$(echo "$LDAP_RESULT" | cut -d: -f1)
	TARGET_GID=$(echo "$LDAP_RESULT" | cut -d: -f2)
	echo "UID: $TARGET_UID, GID: $TARGET_GID"

	groupadd -g "$TARGET_GID" "$TARGET_USERNAME" || true
	useradd -u "$TARGET_UID" -g "$TARGET_GID" -M -s /bin/bash "$TARGET_USERNAME" || true
	RUN_AS_UID=(sudo -E -u "$TARGET_USERNAME")
else
	echo "Could not lookup username '$TARGET_USERNAME' from LDAP or invalid format: '$LDAP_RESULT', using fallback"
	TARGET_UID="616617"
	TARGET_GID="18951"
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
$RUN_AS_UID mkdir -p "$USER_TMP/.cache" "$USER_TMP/.cache/conda/proc" "$USER_TMP/.cache/conda/logs" "$USER_TMP/.cache/conda/notices"

chown -R "$TARGET_USERNAME:$TARGET_USERNAME" "$USER_TMP"
chmod -R 755 "$USER_TMP"

echo "Debug: Checking ownership and permissions of cache directories..."
ls -la "$USER_TMP/.cache/conda/" 2>/dev/null || echo "Cache directory not accessible"
ls -la "$USER_TMP/.cache/conda/notices" 2>/dev/null || echo "Notices directory not accessible"

# Verify writability
[ -w "$USER_TMP" ] || {
	echo "ERROR: $USER_TMP not writable"
	exit 1
}
[ -w "$USER_TMP/conda-tmp" ] || {
	echo "ERROR: $USER_TMP/conda-tmp not writable"
	exit 1
}

# (Optional but harmless) create some subdirs; conda will manage its own layout anyway
$RUN_AS_UID bash -c "
mkdir -p '$USER_PKGS' '$USER_ENVS/.conda' 2>/dev/null || true
"
chown -R "$TARGET_USERNAME:$TARGET_USERNAME" "$USER_PKGS" "$USER_ENVS"
chmod -R 755 "$USER_PKGS" "$USER_ENVS"

# Clean stale locks
$RUN_AS_UID find "$USER_TMP" -name "*.lock" -delete 2>/dev/null || true
$RUN_AS_UID find "$USER_TMP" -name "conda*" -delete 2>/dev/null || true

# Create conda configuration file (expand variables here so .condarc has absolute paths)
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

# Verify conda basics
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
/opt/conda/bin/conda --version
/opt/conda/bin/conda env --help >/dev/null
"

# Set up paths - ensure no double slashes
ENV_PATH="${LOCATION_ROOT%/}/$ENV_NAME"
ENV_DIR_NAME="$ENV_DIR"
ENV_FILE_NAME="$ENV_FILE"
ENV_YAML_PATH=""

echo "Building environment '$ENV_NAME' at '$ENV_PATH'"

# Ensure parent & env directories exist
PARENT_DIR=$(dirname "$ENV_PATH")
if [ ! -d "$PARENT_DIR" ]; then
	echo "Creating parent directory: $PARENT_DIR"
	$RUN_AS_UID mkdir -p "$PARENT_DIR"
fi
if [ ! -d "$ENV_PATH" ]; then
	$RUN_AS_UID mkdir -p "$ENV_PATH"
fi

# Function to check if environment needs updates (no-op detector)
check_env_needs_update() {
	local env_path="$1"
	local yaml_path="$2"

	echo "Checking if environment needs updates..."

	local output_file="$USER_TMP/conda_dry_run.log"
	local error_file="$USER_TMP/conda_dry_run_error.log"

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
/opt/conda/bin/conda env update --prefix '$env_path' --file '$yaml_path' --freeze-installed --offline --dry-run
" >"$output_file" 2>"$error_file"; then
		if grep -q -E "All requested packages already installed|Transaction will be empty" "$output_file"; then
			echo "No changes detected; skipping update."
			return 1 # No changes needed
		else
			echo "Changes detected, proceeding with update..."
			return 0 # Changes needed
		fi
	else
		echo "Dry run failed (likely offline), proceeding with update..."
		return 0
	fi
}

# Function to run conda with logging
run_conda() {
	local output_file="$USER_TMP/conda_output.log"
	local error_file="$USER_TMP/conda_error.log"

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
export OLDPWD='$USER_TMP'
export HOME='$USER_TMP'
export XDG_CACHE_HOME='$USER_TMP/.cache'
export CONDA_ALWAYS_COPY='1'
export CONDA_ALWAYS_YES='true'
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

# Helper: update environment using conda
update_env() {
	local env_path="$1"
	local yaml_path="$2"

	echo "Updating environment at $env_path..."

	# First try offline update
	if run_conda env update --prefix "$env_path" --file "$yaml_path" --freeze-installed --offline -y; then
		echo "✓ Environment updated successfully (offline)"
		return 0
	else
		echo "Offline update failed, trying online update..."
		if run_conda env update --prefix "$env_path" --file "$yaml_path" --freeze-installed -y; then
			echo "✓ Environment updated successfully (online)"
			return 0
		else
			echo "ERROR: Failed to update environment"
			return 1
		fi
	fi
}

# Helper: create environment using conda
create_env() {
	local env_path="$1"
	local yaml_path="$2"

	echo "Creating environment at $env_path..."

	if run_conda env create --prefix "$env_path" --file "$yaml_path" -y; then
		echo "✓ Environment created successfully"
		return 0
	else
		echo "ERROR: Failed to create environment"
		return 1
	fi
}

# Function to run commands within a conda environment
run_in_env() {
	local env_path="$1"
	shift
	echo "Running command in environment: $env_path"
	echo "Command: $*"

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

# Build workspace
BUILD_DIR="/tmp/conda-env-builds-$(date +%s)-$$"
$RUN_AS_UID find /tmp -maxdepth 1 -name "conda-env-builder-${TARGET_USERNAME}-*" -type d -mmin +10 -exec rm -rf {} \; 2>/dev/null || true
$RUN_AS_UID find /tmp -maxdepth 1 -name "conda-env-builds-*" -type d -mmin +10 -exec rm -rf {} \; 2>/dev/null || true

$RUN_AS_UID mkdir -p "$BUILD_DIR"
chown -R "$TARGET_USERNAME:$TARGET_USERNAME" "$BUILD_DIR"
chmod -R 755 "$BUILD_DIR"

WORK_DIR="${BUILD_DIR}/work"
$RUN_AS_UID mkdir -p "$WORK_DIR"
chown -R "$TARGET_USERNAME:$TARGET_USERNAME" "$WORK_DIR"
chmod -R 755 "$WORK_DIR"

# Clone the repository
$RUN_AS_UID git clone https://github.com/PurdueAF/purdue-af-conda-envs.git "$WORK_DIR"

# Verify the environment directory and file exist
if ! $RUN_AS_UID bash -c "cd '$WORK_DIR' && [ -d '${ENV_DIR}' ]"; then
	echo "ERROR: Environment directory ${ENV_DIR} not found!"
	exit 1
fi

ENV_YAML_PATH="${WORK_DIR}/${ENV_DIR}/${ENV_FILE}"
if [ ! -f "$ENV_YAML_PATH" ]; then
	echo "ERROR: Environment file not found at $ENV_YAML_PATH"
	exit 1
fi

# Copy environment file
ENV_YAML_COPY="${USER_TMP}/env/environment.yaml"
$RUN_AS_UID mkdir -p "$(dirname "$ENV_YAML_COPY")"
$RUN_AS_UID cp "$ENV_YAML_PATH" "$ENV_YAML_COPY"

# --- CRITICAL FIX: sanitize YAML to avoid 'could not parse "- conda-forge"' ---
# Remove UTF-8 BOM, Windows CR, and literal TABs (YAML forbids tabs in indentation).
$RUN_AS_UID bash -c "
set -e
YFILE='$ENV_YAML_COPY'
# Strip BOM if present
if [ \"\$(head -c 3 \"\$YFILE\" 2>/dev/null)\" = \$'\xEF\xBB\xBF' ]; then
  tail -c +4 \"\$YFILE\" > \"\$YFILE.tmp\" && mv \"\$YFILE.tmp\" \"\$YFILE\"
fi
# Normalize line endings
tr -d '\r' < \"\$YFILE\" > \"\$YFILE.tmp\" && mv \"\$YFILE.tmp\" \"\$YFILE\"
# Replace tabs with two spaces
TAB=\$(printf '\t')
if grep -q \"\$TAB\" \"\$YFILE\"; then
  sed -i \"s/\$TAB/  /g\" \"\$YFILE\"
fi
"

echo "Using environment.yaml file as-is (post-sanitize)"

# Debug info
echo "=== DEBUG: YAML file info ==="
echo "Source file: $ENV_YAML_PATH"
echo "Target file: $ENV_YAML_COPY"
echo "File size: $(wc -c <"$ENV_YAML_COPY")"
ls -la "$ENV_YAML_COPY"
echo "First 20 lines of YAML file:"
head -20 "$ENV_YAML_COPY"
echo "=== END DEBUG ==="

# Create or update the environment (never delete existing env)
if [ -d "$ENV_PATH" ] && [ -d "$ENV_PATH/conda-meta" ] && [ -f "$ENV_PATH/conda-meta/history" ]; then
	echo "Updating existing environment: $ENV_NAME"
	if check_env_needs_update "$ENV_PATH" "$ENV_YAML_COPY"; then
		if update_env "$ENV_PATH" "$ENV_YAML_COPY"; then
			echo "✓ Environment updated successfully"
		else
			echo "ERROR: Failed to update environment: $ENV_NAME"
			exit 1
		fi
	fi
else
	echo "Creating new environment: $ENV_NAME"
	if create_env "$ENV_PATH" "$ENV_YAML_COPY"; then
		echo "✓ Environment created successfully"
	else
		echo "ERROR: Failed to create environment: $ENV_NAME"
		exit 1
	fi
fi

# Handle pip uninstall if specified
if [ -n "$PIP_UNINSTALL_FILE" ]; then
	if [ -f "${WORK_DIR}/${ENV_DIR}/${PIP_UNINSTALL_FILE}" ]; then
		echo "Uninstalling packages from ${PIP_UNINSTALL_FILE}"
		while IFS= read -r package || [ -n "$package" ]; do
			package=$(echo "$package" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
			if [[ -n "$package" && ! "$package" =~ ^[[:space:]]*# ]]; then
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
		done <"${WORK_DIR}/${ENV_DIR}/${PIP_UNINSTALL_FILE}"
		echo "Pip uninstall completed"
	fi
fi

# Clean up
$RUN_AS_UID rm -rf "$WORK_DIR"
$RUN_AS_UID find "$BUILD_DIR" -name "*.tmp" -delete 2>/dev/null || true
$RUN_AS_UID find "$BUILD_DIR" -name "*.lock" -delete 2>/dev/null || true
$RUN_AS_UID find "$BUILD_DIR" -name "*.cache" -delete 2>/dev/null || true

# Final cleanup of temporary directories
$RUN_AS_UID find /tmp -maxdepth 1 -name "conda-env-builder-${TARGET_USERNAME}-*" -type d -mmin +5 -exec rm -rf {} \; 2>/dev/null || true
$RUN_AS_UID find /tmp -maxdepth 1 -name "conda-env-builds-*" -type d -mmin +5 -exec rm -rf {} \; 2>/dev/null || true

# Final verification of the built environment
if [ -d "$ENV_PATH" ] && [ -d "$ENV_PATH/conda-meta" ]; then
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
