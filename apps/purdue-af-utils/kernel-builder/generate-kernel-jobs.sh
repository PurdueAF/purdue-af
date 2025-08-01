#!/bin/bash

set -e

echo "Starting kernel job generator..."

# Install required packages
echo "Installing required packages..."
dnf install -y git python3-pip wget file bzip2 diffutils --nogpgcheck

# Install kubectl
echo "Installing kubectl..."
curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
chmod +x kubectl
mv kubectl /usr/local/bin/

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

# Function to validate environment name
validate_env_name() {
	local env_name="$1"
	if [[ "$env_name" =~ \.\. ]] || [[ "$env_name" =~ / ]] || [[ "$env_name" =~ ^[[:space:]]*$ ]]; then
		echo "ERROR: Invalid environment name '$env_name' - contains path traversal characters or is empty"
		return 1
	fi
	return 0
}

# Function to sanitize environment name for Kubernetes
sanitize_env_name() {
	local env_name="$1"
	# Replace underscores and other invalid characters with hyphens
	# Remove any leading/trailing non-alphanumeric characters
	echo "$env_name" | sed 's/[^a-z0-9]/-/g' | sed 's/^[^a-z0-9]*//' | sed 's/[^a-z0-9]*$//' | sed 's/--*/-/g' | sed 's/^-//' | sed 's/-$//'
}

# Function to create job YAML for a single environment
create_job_yaml() {
	local env_name="$1"
	local env_dir="$2"
	local env_file="$3"
	local sanitized_name=$(sanitize_env_name "$env_name")

	printf 'apiVersion: batch/v1\n'
	printf 'kind: Job\n'
	printf 'metadata:\n'
	printf '  name: kernel-builder-%s\n' "$sanitized_name"
	printf '  namespace: cms\n'
	printf '  labels:\n'
	printf '    app: kernel-builder\n'
	printf '    environment: %s\n' "$env_name"
	printf 'spec:\n'
	printf '  template:\n'
	printf '    spec:\n'
	printf '      serviceAccountName: kernel-builder\n'
	printf '      restartPolicy: Never\n'
	printf '      containers:\n'
	printf '        - name: kernel-builder\n'
	printf '          image: rockylinux:8.9\n'
	printf '          command:\n'
	printf '            - /bin/bash\n'
	printf '            - -c\n'
	printf '            - |\n'
	printf '              # Copy the build script and execute it for this specific environment\n'
	printf '              cp /scripts/build-single-kernel.sh /tmp/build-single-kernel.sh\n'
	printf '              chmod +x /tmp/build-single-kernel.sh\n'
	printf '              /tmp/build-single-kernel.sh "%s" "%s" "%s"\n' "$env_name" "$env_dir" "$env_file"
	printf '          resources:\n'
	printf '            requests:\n'
	printf '              memory: "4Gi"\n'
	printf '              cpu: "2"\n'
	printf '            limits:\n'
	printf '              memory: "8Gi"\n'
	printf '              cpu: "4"\n'
	printf '          volumeMounts:\n'
	printf '            - name: af-shared-storage\n'
	printf '              mountPath: /work/\n'
	printf '              mountPropagation: HostToContainer\n'
	printf '            - name: scripts\n'
	printf '              mountPath: /scripts/\n'
	printf '              readOnly: true\n'
	printf '      volumes:\n'
	printf '        - name: af-shared-storage\n'
	printf '          persistentVolumeClaim:\n'
	printf '            claimName: af-shared-storage\n'
	printf '        - name: scripts\n'
	printf '          configMap:\n'
	printf '            name: kernel-builder-scripts\n'
	printf '      nodeSelector:\n'
	printf '        cms-af-prod: "true"\n'
	printf '      tolerations:\n'
	printf '        - key: "hub.jupyter.org/dedicated"\n'
	printf '          operator: "Equal"\n'
	printf '          value: "cms-af"\n'
	printf '          effect: "NoSchedule"\n'
}

# Find all directories and create jobs for them
echo "Scanning for directories with environment.yaml files..."
for dir in */; do
	if [ -d "$dir" ]; then
		env_name=$(basename "$dir")
		env_dir="${dir%/}"

		echo "Processing directory: $env_dir"
		echo "Environment name: $env_name"
		sanitized_name=$(sanitize_env_name "$env_name")
		echo "Sanitized name for job: $sanitized_name"

		# Validate environment name
		if ! validate_env_name "$env_name"; then
			echo "Skipping invalid environment: $env_name"
			continue
		fi

		# Check if environment.yaml or environment.yml exists
		env_file=""
		if [ -f "${env_dir}/environment.yaml" ]; then
			env_file="environment.yaml"
		elif [ -f "${env_dir}/environment.yml" ]; then
			env_file="environment.yml"
		fi

		if [ -n "$env_file" ]; then
			echo "Found $env_file in $env_dir, checking for existing jobs..."

			# Check if there's already a running job for this environment
			existing_jobs=$(kubectl get jobs -n cms -l "app=kernel-builder,environment=$env_name" --field-selector=status.successful!=1,status.failed!=1 -o name 2>/dev/null || echo "")

			if [ -n "$existing_jobs" ]; then
				echo "Skipping $env_name - job already running or pending"
				continue
			fi

			echo "Creating job for environment: $env_name..."

			# Create job YAML
			job_yaml=$(create_job_yaml "$env_name" "$env_dir" "$env_file")

			# Apply the job
			echo "$job_yaml" | kubectl apply -f -

			if [ $? -eq 0 ]; then
				echo "Successfully created job for environment: $env_name"
			else
				echo "Failed to create job for environment: $env_name"
			fi
		else
			echo "No environment.yaml or environment.yml found in $env_dir, skipping..."
		fi
	fi
done

# Clean up completed jobs (keep only the current job per environment)
echo "Cleaning up completed jobs..."
for env_name in $(kubectl get jobs -n cms -l app=kernel-builder -o jsonpath='{.items[*].metadata.labels.environment}' | tr ' ' '\n' | sort -u); do
	echo "Checking completed jobs for environment: $env_name"
	# Get completed jobs (successful or failed)
	completed_jobs=$(kubectl get jobs -n cms -l "app=kernel-builder,environment=$env_name" --field-selector=status.successful=1 -o name 2>/dev/null || echo "")
	completed_jobs="$completed_jobs $(kubectl get jobs -n cms -l "app=kernel-builder,environment=$env_name" --field-selector=status.failed=1 -o name 2>/dev/null || echo "")"

	if [ -n "$completed_jobs" ]; then
		echo "$completed_jobs" | while read job; do
			if [ -n "$job" ]; then
				echo "Deleting completed job: $job"
				kubectl delete -n cms "$job"
			fi
		done
	else
		echo "No completed jobs to delete for environment: $env_name"
	fi
done

# Clean up
rm -rf /tmp/purdue-af-kernels

echo "Kernel job generation completed"
