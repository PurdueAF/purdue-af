#!/bin/bash

set -e

# Check arguments (location_root required, fifth arg optional: pip uninstall file)
if [ $# -lt 4 ] || [ $# -gt 5 ]; then
	echo "Usage: $0 <environment_name> <environment_directory> <environment_file> <location_root> [pip_uninstall_file]"
	exit 1
fi

ENV_NAME="$1"
ENV_DIR="$2"
ENV_FILE="$3"
LOCATION_ROOT="$4"
PIP_UNINSTALL_FILE="$5"

echo "Starting single kernel builder for environment: $ENV_NAME"

# Install required packages
echo "Installing required packages..."
dnf install -y git python3-pip wget file bzip2 diffutils --nogpgcheck

# Install micromamba for faster conda operations
echo "Downloading micromamba..."
# Detect architecture and download appropriate binary
export PATH="/usr/local/bin:$PATH"

ARCH=$(uname -m)
echo "Detected architecture: $ARCH"

case "$ARCH" in
x86_64)
	PLATFORM="linux-64"
	;;
aarch64 | arm64)
	PLATFORM="linux-aarch64"
	;;
*)
	echo "Unsupported architecture: $ARCH"
	exit 1
	;;
esac

echo "Downloading micromamba for $PLATFORM..."
wget -O micromamba.tar.bz2 "https://micro.mamba.pm/api/micromamba/$PLATFORM/latest"

echo "Extracting micromamba..."
tar -xvjf micromamba.tar.bz2
echo "Checking extracted files:"
ls -la

# Find the micromamba binary (it might be in a subdirectory)
if [ -f "micromamba" ]; then
	echo "Found micromamba in current directory"
elif [ -f "bin/micromamba" ]; then
	echo "Found micromamba in bin/ directory"
	mv bin/micromamba micromamba
else
	echo "Looking for micromamba binary..."
	find . -name "micromamba" -type f
	MICROMAMBA_PATH=$(find . -name "micromamba" -type f | head -1)
	if [ -n "$MICROMAMBA_PATH" ]; then
		echo "Found micromamba at: $MICROMAMBA_PATH"
		mv "$MICROMAMBA_PATH" micromamba
	else
		echo "ERROR: Could not find micromamba binary after extraction"
		exit 1
	fi
fi

chmod +x micromamba
echo "Moving micromamba to /usr/local/bin/"
mv micromamba /usr/local/bin/

# Clone the repository
echo "Cloning purdue-af-kernels repository..."
git clone https://github.com/PurdueAF/purdue-af-kernels.git /tmp/purdue-af-kernels
cd /tmp/purdue-af-kernels

# Validate environment name
if [[ "$ENV_NAME" =~ \.\. ]] || [[ "$ENV_NAME" =~ / ]] || [[ "$ENV_NAME" =~ ^[[:space:]]*$ ]]; then
	echo "ERROR: Invalid environment name '$ENV_NAME' - contains path traversal characters or is empty"
	exit 1
fi

# Set up environment path (location-aware)
ENV_PATH="${LOCATION_ROOT}/$ENV_NAME"
ENV_YAML_PATH="${ENV_DIR}/${ENV_FILE}"

echo "Environment name: $ENV_NAME"
echo "Environment path: $ENV_PATH"
echo "Environment file: $ENV_FILE"
echo "Environment file path: $ENV_YAML_PATH"
echo "Location root: $LOCATION_ROOT"
echo "PIP uninstall file: $PIP_UNINSTALL_FILE"

# Resolve pip uninstall path (if provided)
if [ -n "$PIP_UNINSTALL_FILE" ]; then
	PIP_UNINSTALL_PATH="${ENV_DIR}/${PIP_UNINSTALL_FILE}"
else
	PIP_UNINSTALL_PATH=""
fi

# Compute fingerprint helper
compute_fingerprint() {
	local env_path="$1"
	local env_yaml_path="$2"
	local pip_uninstall_path="$3"

	local yaml_sha pipun_sha conda_state pip_state
	yaml_sha=$(sha256sum "$env_yaml_path" | awk '{print $1}')
	if [ -n "$pip_uninstall_path" ] && [ -f "$pip_uninstall_path" ]; then
		pipun_sha=$(sha256sum "$pip_uninstall_path" | awk '{print $1}')
	else
		pipun_sha="none"
	fi

	# Gather current state if env is valid
	conda_state=""
	pip_state=""
	if micromamba list -p "$env_path" >/dev/null 2>&1; then
		conda_state=$(micromamba list -p "$env_path" --explicit 2>/dev/null || true)
		if [ -x "$env_path/bin/python" ]; then
			pip_state=$("$env_path/bin/python" -m pip freeze --all 2>/dev/null || true)
		else
			pip_state=$(micromamba run -p "$env_path" python -m pip freeze --all 2>/dev/null || true)
		fi
	fi

	# Normalize by ensuring trailing newlines
	printf '%s\n%s\n%s\n' "$yaml_sha" "$pipun_sha" "$conda_state" | sha256sum | awk '{print $1}'
}

# Ensure metadata directory exists post-build
write_metadata() {
	local env_path="$1"
	local env_yaml_path="$2"
	local pip_uninstall_path="$3"

	mkdir -p "$env_path/.af"
	# Save current state snapshots
	micromamba list -p "$env_path" --explicit > "$env_path/.af/conda-explicit.txt" 2>/dev/null || true
	if [ -x "$env_path/bin/python" ]; then
		"$env_path/bin/python" -m pip freeze --all > "$env_path/.af/pip-freeze.txt" 2>/dev/null || true
	else
		micromamba run -p "$env_path" python -m pip freeze --all > "$env_path/.af/pip-freeze.txt" 2>/dev/null || true
	fi
	local fp
	fp=$(compute_fingerprint "$env_path" "$env_yaml_path" "$pip_uninstall_path")
	echo "$fp" > "$env_path/.af/af_fingerprint"
	local yaml_sha pipun_sha
	yaml_sha=$(sha256sum "$env_yaml_path" | awk '{print $1}')
	if [ -n "$pip_uninstall_path" ] && [ -f "$pip_uninstall_path" ]; then
		pipun_sha=$(sha256sum "$pip_uninstall_path" | awk '{print $1}')
	else
		pipun_sha="none"
	fi
	printf '{"env_yaml_sha":"%s","pip_uninstall_sha":"%s","fingerprint":"%s","ts":"%s"}\n' \
		"$yaml_sha" "$pipun_sha" "$fp" "$(date -u +%FT%TZ)" > "$env_path/.af/meta.json"
}

# Check if environment file exists
if [ ! -f "$ENV_YAML_PATH" ]; then
	echo "ERROR: $ENV_FILE not found at $ENV_YAML_PATH"
	exit 1
fi

echo "Found environment.yaml, processing environment..."

# Check if environment already exists and is valid
if [ -d "$ENV_PATH" ]; then
	echo "Environment $ENV_NAME already exists, checking validity..."
	# Check if the environment is valid by trying to list packages
	if micromamba list -p "$ENV_PATH" >/dev/null 2>&1; then
		# Compare fingerprints to decide if rebuild is needed
		CURRENT_FP=$(compute_fingerprint "$ENV_PATH" "$ENV_YAML_PATH" "$PIP_UNINSTALL_PATH")
		SAVED_FP=""
		if [ -f "$ENV_PATH/.af/af_fingerprint" ]; then
			SAVED_FP=$(cat "$ENV_PATH/.af/af_fingerprint" 2>/dev/null || echo "")
		fi
		if [ -n "$SAVED_FP" ] && [ "$CURRENT_FP" = "$SAVED_FP" ]; then
			echo "Environment $ENV_NAME at $ENV_PATH is up-to-date (fingerprint match). Skipping rebuild."
			exit 0
		fi
		echo "Environment $ENV_NAME is valid, updating..."
		# Copy the new environment file for tracking
		cp "$ENV_YAML_PATH" "$ENV_PATH/"
		chmod 644 "$ENV_PATH/$ENV_FILE"
		# Update the existing environment
		if micromamba env update -f "$ENV_YAML_PATH" -p "$ENV_PATH" --yes; then
			echo "Successfully updated environment: $ENV_NAME"
		else
			echo "Failed to update environment: $ENV_NAME"
			exit 1
		fi
	else
		echo "Environment $ENV_NAME exists but is invalid. Recreating..."
		echo "Cleaning up environment directory: $ENV_PATH"
		rm -rf "$ENV_PATH"
		# Force remove with find to handle any stubborn files
		find "$(dirname "$ENV_PATH")" -name "$(basename "$ENV_PATH")" -type d -exec rm -rf {} + 2>/dev/null || true
		# Wait a moment to ensure filesystem sync
		sleep 2
		echo "Checking if directory still exists:"
		ls -la "$(dirname "$ENV_PATH")" | grep "$(basename "$ENV_PATH")" || echo "Directory successfully removed"

		echo "Creating new conda environment: $ENV_NAME"
		if micromamba env create -f "$ENV_YAML_PATH" -p "$ENV_PATH" --yes; then
			echo "Successfully created environment: $ENV_NAME"
			# Copy environment file to the created environment for tracking
			cp "$ENV_YAML_PATH" "$ENV_PATH/"
			chmod 644 "$ENV_PATH/$ENV_FILE"
		else
			echo "Failed to create environment: $ENV_NAME, performing aggressive cleanup..."
			echo "Current directory contents:"
			ls -la "$ENV_PATH" || echo "Directory does not exist"

			# More aggressive cleanup
			rm -rf "$ENV_PATH"
			find "$(dirname "$ENV_PATH")" -name "$(basename "$ENV_PATH")" -type d -exec rm -rf {} + 2>/dev/null || true
			find "$(dirname "$ENV_PATH")" -name "$(basename "$ENV_PATH")" -type f -delete 2>/dev/null || true
			sleep 3

			echo "After cleanup, checking directory:"
			ls -la "$(dirname "$ENV_PATH")" | grep "$(basename "$ENV_PATH")" || echo "Directory successfully removed"

			if micromamba env create -f "$ENV_YAML_PATH" -p "$ENV_PATH" --yes; then
				echo "Successfully created environment: $ENV_NAME on retry"
				# Copy environment file to the created environment for tracking
				cp "$ENV_YAML_PATH" "$ENV_PATH/"
				chmod 644 "$ENV_PATH/$ENV_FILE"
			else
				echo "Failed to create environment: $ENV_NAME even after aggressive cleanup"
				echo "Final directory contents:"
				ls -la "$ENV_PATH" || echo "Directory does not exist"
				exit 1
			fi
		fi
	fi
else
	echo "Environment $ENV_NAME does not exist, creating new environment..."
	# Create new conda environment
	echo "Creating new conda environment: $ENV_NAME"
	if micromamba env create -f "$ENV_YAML_PATH" -p "$ENV_PATH" --yes; then
		echo "Successfully created environment: $ENV_NAME"
		# Copy environment file to the created environment for tracking
		cp "$ENV_YAML_PATH" "$ENV_PATH/"
		chmod 644 "$ENV_PATH/$ENV_FILE"
	else
		echo "Failed to create environment: $ENV_NAME, performing aggressive cleanup..."
		echo "Current directory contents:"
		ls -la "$ENV_PATH" || echo "Directory does not exist"

		# More aggressive cleanup
		rm -rf "$ENV_PATH"
		find "$(dirname "$ENV_PATH")" -name "$(basename "$ENV_PATH")" -type d -exec rm -rf {} + 2>/dev/null || true
		find "$(dirname "$ENV_PATH")" -name "$(basename "$ENV_PATH")" -type f -delete 2>/dev/null || true
		sleep 3

		echo "After cleanup, checking directory:"
		ls -la "$(dirname "$ENV_PATH")" | grep "$(basename "$ENV_PATH")" || echo "Directory successfully removed"

		if micromamba env create -f "$ENV_YAML_PATH" -p "$ENV_PATH" --yes; then
			echo "Successfully created environment: $ENV_NAME on retry"
			# Copy environment file to the created environment for tracking
			cp "$ENV_YAML_PATH" "$ENV_PATH/"
			chmod 644 "$ENV_PATH/$ENV_FILE"
		else
			echo "Failed to create environment: $ENV_NAME even after aggressive cleanup"
			echo "Final directory contents:"
			ls -la "$ENV_PATH" || echo "Directory does not exist"
			exit 1
		fi
	fi
fi

# If pip-uninstall.txt was provided and exists under the env directory, uninstall packages from the created/updated env
if [ -n "$PIP_UNINSTALL_PATH" ] && [ -f "$PIP_UNINSTALL_PATH" ]; then
	echo "Uninstalling packages from $PIP_UNINSTALL_PATH"
	if [ -x "$ENV_PATH/bin/python" ]; then
		"$ENV_PATH/bin/python" -m pip uninstall -r "$PIP_UNINSTALL_PATH" -y
	else
		micromamba run -p "$ENV_PATH" python -m pip uninstall -r "$PIP_UNINSTALL_PATH" -y
	fi
fi

# After successful build/update and optional pip uninstalls, write metadata and fingerprint
write_metadata "$ENV_PATH" "$ENV_YAML_PATH" "$PIP_UNINSTALL_PATH"

# Clean up
rm -rf /tmp/purdue-af-kernels

echo "Kernel building completed for environment: $ENV_NAME"
