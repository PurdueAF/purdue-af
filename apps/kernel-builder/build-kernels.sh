#!/bin/bash

echo "Starting kernel builder script..."

# Install required packages
echo "Installing required packages..."
dnf install -y git python3-pip wget diffutils --nogpgcheck

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
  aarch64|arm64)
    PLATFORM="linux-aarch64"
    ;;
  *)
    echo "Unsupported architecture: $ARCH"
    exit 1
    ;;
esac

echo "Downloading micromamba for $PLATFORM..."
wget -O micromamba "https://micro.mamba.pm/api/micromamba/$PLATFORM/latest"

echo "Checking downloaded file..."
ls -la micromamba
file micromamba
echo "File type details:"
ldd micromamba 2>/dev/null || echo "ldd not available or not a dynamic binary"

echo "Setting permissions on micromamba..."
chmod +x micromamba
echo "Moving micromamba to /usr/local/bin/"
mv micromamba /usr/local/bin/
echo "Testing micromamba..."
/usr/local/bin/micromamba --version
echo "Micromamba installation completed"

# Clone the repository
echo "Cloning purdue-af-kernels repository..."
git clone https://github.com/PurdueAF/purdue-af-kernels.git /tmp/purdue-af-kernels
cd /tmp/purdue-af-kernels

# Function to build environment from directory
build_environment() {
	local dir="$1"
	local env_name=$(basename "$dir")
	local env_path="/work/kernels/$env_name"

	echo "Processing directory: $dir"
	echo "Environment name: $env_name"
	echo "Environment path: $env_path"

	if [ -f "${dir%/}/environment.yaml" ]; then
		echo "Found environment.yaml in $dir"

		# Check if environment already exists and is up to date
		if [ -d "$env_path" ]; then
			echo "Environment $env_name already exists, updating..."
			# Copy the new environment.yaml for tracking
			cp "${dir%/}/environment.yaml" "$env_path/"
			chmod 644 "$env_path/environment.yaml"
			# Update the existing environment
			if micromamba env update -f "${dir%/}/environment.yaml" -p "$env_path" --yes; then
				echo "Successfully updated environment: $env_name"
			else
				echo "Failed to update environment: $env_name"
				return 1
			fi
		else
			echo "Environment $env_name does not exist, creating new environment..."
			# Create environment directory with proper permissions
			mkdir -p "$env_path"
			chmod 755 "$env_path"

			# Copy environment.yaml to the environment directory for tracking
			cp "${dir%/}/environment.yaml" "$env_path/"
			chmod 644 "$env_path/environment.yaml"

			# Create new conda environment
			echo "Creating new conda environment: $env_name"
			if micromamba env create -f "${dir%/}/environment.yaml" -p "$env_path" --yes; then
				echo "Successfully created environment: $env_name"
			else
				echo "Failed to create environment: $env_name"
				return 1
			fi
		fi
	else
		echo "No environment.yaml found in $dir"
	fi
}

# Find all directories and process them
echo "Scanning for directories with environment.yaml files..."
for dir in */; do
	if [ -d "$dir" ]; then
		build_environment "$dir"
	fi
done

# Clean up
rm -rf /tmp/purdue-af-kernels

echo "Kernel building job completed"
