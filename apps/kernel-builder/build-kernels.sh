#!/bin/bash

echo "Starting kernel builder script..."

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
chmod +x micromamba

echo "Moving micromamba to /usr/local/bin/"
mv micromamba /usr/local/bin/

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
			echo "Environment $env_name already exists, checking validity..."
			# Check if the environment is valid by trying to list packages
			if micromamba list -p "$env_path" >/dev/null 2>&1; then
				echo "Environment $env_name is valid, updating..."
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
				echo "Environment $env_name exists but is invalid. Recreating..."
				rm -rf "$env_path"
				mkdir -p "$env_path"
				cp "${dir%/}/environment.yaml" "$env_path/"
				chmod 644 "$env_path/environment.yaml"
				echo "Creating new conda environment: $env_name"
				if micromamba env create -f "${dir%/}/environment.yaml" -p "$env_path" --yes; then
					echo "Successfully created environment: $env_name"
				else
					echo "Failed to create environment: $env_name, cleaning up and retrying..."
					rm -rf "$env_path"
					mkdir -p "$env_path"
					cp "${dir%/}/environment.yaml" "$env_path/"
					chmod 644 "$env_path/environment.yaml"
					if micromamba env create -f "${dir%/}/environment.yaml" -p "$env_path" --yes; then
						echo "Successfully created environment: $env_name on retry"
					else
						echo "Failed to create environment: $env_name even after cleanup"
						return 1
					fi
				fi
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
				echo "Failed to create environment: $env_name, cleaning up and retrying..."
				rm -rf "$env_path"
				mkdir -p "$env_path"
				cp "${dir%/}/environment.yaml" "$env_path/"
				chmod 644 "$env_path/environment.yaml"
				if micromamba env create -f "${dir%/}/environment.yaml" -p "$env_path" --yes; then
					echo "Successfully created environment: $env_name on retry"
				else
					echo "Failed to create environment: $env_name even after cleanup"
					return 1
				fi
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

# Clean up orphaned kernel directories
echo "Cleaning up orphaned kernel directories..."
cleanup_orphaned_kernels() {
	local repo_dirs=()
	
	# Get list of directories in the repository
	for dir in */; do
		if [ -d "$dir" ]; then
			repo_dirs+=("$(basename "$dir")")
		fi
	done
	
	# Check each kernel directory in /work/kernels
	if [ -d "/work/kernels" ]; then
		for kernel_dir in /work/kernels/*/; do
			if [ -d "$kernel_dir" ]; then
				local kernel_name=$(basename "$kernel_dir")
				local found=false
				
				# Check if this kernel exists in the repository
				for repo_dir in "${repo_dirs[@]}"; do
					if [ "$kernel_name" = "$repo_dir" ]; then
						found=true
						break
					fi
				done
				
				# If not found in repository, remove it
				if [ "$found" = false ]; then
					echo "Removing orphaned kernel directory: $kernel_name"
					rm -rf "$kernel_dir"
				fi
			fi
		done
	fi
}

cleanup_orphaned_kernels

# Clean up
rm -rf /tmp/purdue-af-kernels

echo "Kernel building job completed"
