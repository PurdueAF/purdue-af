#!/bin/bash

set -e # Exit on any error
# set -x # Print commands as they execute

echo "Starting kernel builder script..."

# Install required packages
echo "Installing required packages..."
dnf install -y git python3-pip wget --nogpgcheck

# Install micromamba for faster conda operations
echo "Downloading micromamba..."
wget -O micromamba "https://micro.mamba.pm/api/micromamba/linux-64/latest"
echo "Setting permissions on micromamba..."
chmod +x micromamba
echo "Moving micromamba to /usr/local/bin/"
mv micromamba /usr/local/bin/
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
			echo "Environment $env_name already exists, checking for updates..."

			# Compare environment.yaml with existing environment
			if cmp -s "${dir%/}/environment.yaml" "$env_path/environment.yaml"; then
				echo "Environment $env_name is up to date, skipping..."
				return 0
			else
				echo "Environment $env_name needs update, rebuilding..."
				rm -rf "$env_path"
			fi
		fi

		# Create environment directory with proper permissions
		mkdir -p "$env_path"
		chmod 755 "$env_path"

		# Copy environment.yaml to the environment directory for tracking
		cp "${dir%/}/environment.yaml" "$env_path/"
		chmod 644 "$env_path/environment.yaml"

		# Build the conda environment
		echo "Building conda environment: $env_name"
		micromamba env create -f "${dir%/}/environment.yaml" -p "$env_path" --yes

		if [ $? -eq 0 ]; then
			echo "Successfully built environment: $env_name"
		else
			echo "Failed to build environment: $env_name"
			return 1
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
