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
echo "Cloning purdue-af-conda-envs repository..."
git clone https://github.com/PurdueAF/purdue-af-conda-envs.git /tmp/purdue-af-conda-envs
cd /tmp/purdue-af-conda-envs

# Function to validate environment name
validate_env_name() {
	local env_name="$1"
	if [[ "$env_name" =~ \.\. ]] || [[ "$env_name" =~ / ]] || [[ "$env_name" =~ ^[[:space:]]*$ ]]; then
		echo "ERROR: Invalid environment name '$env_name' - contains path traversal characters or is empty"
		return 1
	fi
	return 0
}

# Function to create job YAML for a single environment-location pair
create_job_yaml_location() {
	local env_name="$1"
	local env_dir="$2"
	local env_file="$3"
	local pip_uninstall_file="$4"
	local location_root="$5"  # /work/kernels or /depot/cms/kernels
	local location_label="$6" # work or depot
	local sanitized_name=$(sanitize_env_name "$env_name")

	printf 'apiVersion: batch/v1\n'
	printf 'kind: Job\n'
	printf 'metadata:\n'
	printf '  name: kernel-builder-%s-%s\n' "$sanitized_name" "$location_label"
	printf '  namespace: cms\n'
	printf '  labels:\n'
	printf '    app: kernel-builder\n'
	printf '    environment: %s\n' "$env_name"
	printf '    location: %s\n' "$location_label"
	printf '  annotations:\n'
	printf '    kernel-builder.af/fingerprint: "%s"\n' "$(compute_env_fingerprint "$env_dir" "$env_file" "$pip_uninstall_file")"
	printf 'spec:\n'
	printf '  backoffLimit: 0\n'
	printf '  ttlSecondsAfterFinished: 300\n'
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
	printf '              # Copy the build script and execute it for this specific environment and location\n'
	printf '              cp /scripts/build-single-kernel.sh /tmp/build-single-kernel.sh\n'
	printf '              chmod +x /tmp/build-single-kernel.sh\n'
	printf '              /tmp/build-single-kernel.sh "%s" "%s" "%s" "%s" "%s"\n' "$env_name" "$env_dir" "$env_file" "$location_root" "$pip_uninstall_file"
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
	printf '            - name: depot\n'
	printf '              mountPath: /depot/cms\n'
	printf '              mountPropagation: HostToContainer\n'
	printf '            - name: scripts\n'
	printf '              mountPath: /scripts/\n'
	printf '              readOnly: true\n'
	printf '      volumes:\n'
	printf '        - name: af-shared-storage\n'
	printf '          persistentVolumeClaim:\n'
	printf '            claimName: af-shared-storage\n'
	printf '        - name: depot\n'
	printf '          nfs:\n'
	printf '            server: datadepot.rcac.purdue.edu\n'
	printf '            path: /depot/cms\n'
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

# Function to sanitize environment name for Kubernetes
sanitize_env_name() {
	local env_name="$1"
	# Replace underscores and other invalid characters with hyphens
	# Remove any leading/trailing non-alphanumeric characters
	echo "$env_name" | sed 's/[^a-z0-9]/-/g' | sed 's/^[^a-z0-9]*//' | sed 's/[^a-z0-9]*$//' | sed 's/--*/-/g' | sed 's/^-//' | sed 's/-$//'
}

# Function to compute environment fingerprint for triggering job recreation
compute_env_fingerprint() {
	local env_dir="$1"
	local env_file="$2"
	local pip_uninstall_file="$3"

	local yaml_sha pipun_sha
	yaml_sha=$(sha256sum "${env_dir}/${env_file}" | awk '{print $1}')
	if [ -n "$pip_uninstall_file" ] && [ -f "${env_dir}/${pip_uninstall_file}" ]; then
		pipun_sha=$(sha256sum "${env_dir}/${pip_uninstall_file}" | awk '{print $1}')
	else
		pipun_sha="none"
	fi

	# Combine both hashes for a unique fingerprint
	printf '%s-%s' "$yaml_sha" "$pipun_sha" | sha256sum | awk '{print $1}'
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

		# Check if pip-uninstall.txt exists
		pip_uninstall_file=""
		if [ -f "${env_dir}/pip-uninstall.txt" ]; then
			pip_uninstall_file="pip-uninstall.txt"
		fi

		if [ -n "$env_file" ]; then
			echo "Found $env_file in $env_dir, checking for existing jobs..."

			echo "Checking existing jobs for environment: $env_name..."

			# Check for failed jobs and delete them
			failed_jobs_work=$(kubectl get jobs -n cms -l "app=kernel-builder,environment=$env_name,location=work" --field-selector=status.failed=1 -o name 2>/dev/null || echo "")
			failed_jobs_depot=$(kubectl get jobs -n cms -l "app=kernel-builder,environment=$env_name,location=depot" --field-selector=status.failed=1 -o name 2>/dev/null || echo "")

			if [ -n "$failed_jobs_work" ]; then
				echo "Found failed work job(s) for $env_name, deleting them..."
				echo "$failed_jobs_work" | xargs -r kubectl delete -n cms
			fi

			if [ -n "$failed_jobs_depot" ]; then
				echo "Found failed depot job(s) for $env_name, deleting them..."
				echo "$failed_jobs_depot" | xargs -r kubectl delete -n cms
			fi

			# Check for successful jobs
			successful_jobs_work=$(kubectl get jobs -n cms -l "app=kernel-builder,environment=$env_name,location=work" --field-selector=status.successful=1 -o name 2>/dev/null || echo "")
			successful_jobs_depot=$(kubectl get jobs -n cms -l "app=kernel-builder,environment=$env_name,location=depot" --field-selector=status.successful=1 -o name 2>/dev/null || echo "")

			# Check for running/pending jobs
			running_jobs_work=$(kubectl get jobs -n cms -l "app=kernel-builder,environment=$env_name,location=work" --field-selector=status.successful!=1,status.failed!=1 -o name 2>/dev/null || echo "")
			running_jobs_depot=$(kubectl get jobs -n cms -l "app=kernel-builder,environment=$env_name,location=depot" --field-selector=status.successful!=1,status.failed!=1 -o name 2>/dev/null || echo "")

			echo "Creating jobs for environment: $env_name (work and depot)..."

			# Create work job if no running or successful job exists
			if [ -z "$running_jobs_work" ] && [ -z "$successful_jobs_work" ]; then
				job_yaml_work=$(create_job_yaml_location "$env_name" "$env_dir" "$env_file" "$pip_uninstall_file" "/work/kernels" "work")
				echo "$job_yaml_work" | kubectl apply -f -
				if [ $? -eq 0 ]; then
					echo "Successfully created job (work) for environment: $env_name"
				else
					echo "Failed to create job (work) for environment: $env_name"
				fi
			else
				if [ -n "$running_jobs_work" ]; then
					echo "Skipping (work) for $env_name - job already running or pending"
				else
					echo "Skipping (work) for $env_name - job already completed successfully"
				fi
			fi

			# Create depot job if no running or successful job exists
			if [ -z "$running_jobs_depot" ] && [ -z "$successful_jobs_depot" ]; then
				job_yaml_depot=$(create_job_yaml_location "$env_name" "$env_dir" "$env_file" "$pip_uninstall_file" "/depot/cms/kernels" "depot")
				echo "$job_yaml_depot" | kubectl apply -f -
				if [ $? -eq 0 ]; then
					echo "Successfully created job (depot) for environment: $env_name"
				else
					echo "Failed to create job (depot) for environment: $env_name"
				fi
			else
				if [ -n "$running_jobs_depot" ]; then
					echo "Skipping (depot) for $env_name - job already running or pending"
				else
					echo "Skipping (depot) for $env_name - job already completed successfully"
				fi
			fi
		else
			echo "No environment.yaml or environment.yml found in $env_dir, skipping..."
		fi
	fi
done

# Clean up
rm -rf /tmp/purdue-af-conda-envs

echo "Kernel job generation completed"
