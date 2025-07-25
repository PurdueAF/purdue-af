#!/bin/bash

set -e

# Check arguments
if [ $# -ne 3 ]; then
	echo "Usage: $0 <environment_name> <environment_directory> <environment_file>"
	exit 1
fi

ENV_NAME="$1"
ENV_DIR="$2"
ENV_FILE="$3"

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

# Set up environment path
ENV_PATH="/work/kernels/$ENV_NAME"
ENV_YAML_PATH="${ENV_DIR}/${ENV_FILE}"

echo "Environment name: $ENV_NAME"
echo "Environment path: $ENV_PATH"
echo "Environment file: $ENV_FILE"
echo "Environment file path: $ENV_YAML_PATH"

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
		echo "Environment $ENV_NAME is valid, updating..."
		# Copy the new environment.yaml for tracking
		cp "$ENV_YAML_PATH" "$ENV_PATH/"
		chmod 644 "$ENV_PATH/environment.yaml"
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
			# Copy environment.yaml to the created environment for tracking
			cp "$ENV_YAML_PATH" "$ENV_PATH/"
			chmod 644 "$ENV_PATH/environment.yaml"
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
				# Copy environment.yaml to the created environment for tracking
				cp "$ENV_YAML_PATH" "$ENV_PATH/"
				chmod 644 "$ENV_PATH/environment.yaml"
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
		# Copy environment.yaml to the created environment for tracking
		cp "$ENV_YAML_PATH" "$ENV_PATH/"
		chmod 644 "$ENV_PATH/environment.yaml"
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
			# Copy environment.yaml to the created environment for tracking
			cp "$ENV_YAML_PATH" "$ENV_PATH/"
			chmod 644 "$ENV_PATH/environment.yaml"
		else
			echo "Failed to create environment: $ENV_NAME even after aggressive cleanup"
			echo "Final directory contents:"
			ls -la "$ENV_PATH" || echo "Directory does not exist"
			exit 1
		fi
	fi
fi

# Clean up
rm -rf /tmp/purdue-af-kernels

echo "Kernel building completed for environment: $ENV_NAME"
