#!/bin/bash

set -e

# Configuration - Centralized location definitions
# Add or modify locations here as needed
declare -A LOCATIONS=(
	# ["work"]="/work/kernels"
	["depot"]="/depot/cms/kernels"
	["geddes"]="/conda-envs"
)

echo "Starting conda-env job generator..."

# Install required packages with mirror fallbacks
echo "Installing required packages..."
# Try multiple mirror sources to handle Rocky Linux mirror issues
for mirror in "https://mirrors.rockylinux.org" "https://mirror.rockylinux.org" "https://dl.rockylinux.org"; do
	if dnf install -y git python3-pip wget file bzip2 diffutils which --nogpgcheck --setopt=mirrorlist="${mirror}/mirrorlist?arch=x86_64&repo=baseos-8" 2>/dev/null; then
		echo "Successfully installed packages using mirror: $mirror"
		break
	else
		echo "Failed to install packages using mirror: $mirror, trying next..."
	fi
done

# Fallback: try without specific mirror if all mirrors fail
if ! rpm -q git python3-pip wget file bzip2 diffutils which >/dev/null 2>&1; then
	echo "All mirrors failed, trying default dnf configuration..."
	dnf install -y git python3-pip wget file bzip2 diffutils which --nogpgcheck || {
		echo "ERROR: Failed to install required packages even with fallbacks"
		exit 1
	}
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
echo "Verifying symlinks..."
ls -la /usr/local/bin/conda /usr/local/bin/mamba /usr/local/bin/python /usr/local/bin/pip || echo "Some symlinks failed to create"

# Verify conda installation
echo "Verifying conda installation..."
/opt/conda/bin/conda --version

# Verify mamba installation
echo "Verifying mamba installation..."
/opt/conda/bin/mamba --version

# Install kubectl
curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
chmod +x kubectl
mv kubectl /usr/local/bin/

# Clone the repository
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
	local location_root="$5" # Location root path from LOCATIONS array
	local location_label="$6"
	local sanitized_name=$(sanitize_env_name "$env_name")

	printf 'apiVersion: batch/v1\n'
	printf 'kind: Job\n'
	printf 'metadata:\n'
	printf '  name: conda-env-builder-%s-%s\n' "$sanitized_name" "$location_label"
	printf '  namespace: cms\n'
	printf '  labels:\n'
	printf '    app: conda-env-builder\n'
	printf '    environment: %s\n' "$env_name"
	printf '    location: %s\n' "$location_label"
	printf '  annotations:\n'
	printf '    conda-env-builder.af/fingerprint: "%s"\n' "$(compute_env_fingerprint "$env_dir" "$env_file" "$pip_uninstall_file")"
	printf 'spec:\n'
	printf '  backoffLimit: 0\n'
	printf '  ttlSecondsAfterFinished: 1200\n'
	printf '  template:\n'
	printf '    spec:\n'
	printf '      serviceAccountName: conda-env-builder\n'
	printf '      initContainers:\n'
	printf '        - name: init-conda-envs-ownership\n'
	printf '          image: rockylinux:8.9\n'
	printf '          command:\n'
	printf '            - /bin/bash\n'
	printf '            - -c\n'
	printf '            - |\n'
	printf '              echo "Setting permissions for conda-envs directory..."\n'
	printf '              # Get UID/GID for target user (dkondra)\n'
	printf '              TARGET_UID=616617\n'
	printf '              TARGET_GID=18951\n'
	printf '              # Only change ownership of the parent directory, not recursively\n'
	printf '              chown $TARGET_UID:$TARGET_GID /conda-envs/\n'
	printf '              chmod 755 /conda-envs/\n'
	printf '              # Ensure target user can write to the directory\n'
	printf '              echo "Permissions set successfully"\n'
	printf '              ls -ld /conda-envs/\n'
	printf '          volumeMounts:\n'
	printf '            - name: conda-envs\n'
	printf '              mountPath: /conda-envs/\n'
	printf '              mountPropagation: HostToContainer\n'
	printf '          securityContext:\n'
	printf '            runAsUser: 0\n'
	printf '            runAsGroup: 0\n'
	printf '      securityContext:\n'
	printf '        fsGroup: 0\n'
	printf '      restartPolicy: Never\n'
	printf '      containers:\n'
	printf '        - name: conda-env-builder\n'
	printf '          image: rockylinux:8.9\n'
	printf '          command:\n'
	printf '            - /bin/bash\n'
	printf '            - -c\n'
	printf '            - |\n'
	printf '              # Copy the build script and execute it for this specific conda environment and location\n'
	printf '              cp /scripts/build-single-conda-env.sh /tmp/build-single-conda-env.sh\n'
	printf '              chmod +x /tmp/build-single-conda-env.sh\n'
	printf '              /tmp/build-single-conda-env.sh "%s" "%s" "%s" "%s" "%s"\n' "$env_name" "$env_dir" "$env_file" "$location_root" "$pip_uninstall_file"
	printf '          resources:\n'
	printf '            requests:\n'
	printf '              memory: "16Gi"\n'
	printf '              cpu: "2"\n'
	printf '            limits:\n'
	printf '              memory: "32Gi"\n'
	printf '              cpu: "4"\n'
	printf '          volumeMounts:\n'
	printf '            - name: af-shared-storage\n'
	printf '              mountPath: /work/\n'
	printf '              mountPropagation: HostToContainer\n'
	printf '            - name: depot\n'
	printf '              mountPath: /depot/cms\n'
	printf '              mountPropagation: HostToContainer\n'
	printf '            - name: conda-envs\n'
	printf '              mountPath: /conda-envs/\n'
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
	printf '        - name: conda-envs\n'
	printf '          persistentVolumeClaim:\n'
	printf '            claimName: conda-envs\n'
	printf '        - name: scripts\n'
	printf '          configMap:\n'
	printf '            name: conda-env-builder-scripts\n'
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

# Function to check and manage jobs for a specific location
check_and_manage_jobs() {
	local env_name="$1"
	local location_label="$2"

	echo "Checking jobs for environment: $env_name at location: $location_label"

	# Check for failed jobs and delete them
	local failed_jobs=$(kubectl get jobs -n cms -l "app=conda-env-builder,environment=$env_name,location=$location_label" --field-selector=status.failed=1 -o name 2>/dev/null || echo "")
	if [ -n "$failed_jobs" ]; then
		echo "Found failed $location_label job(s) for $env_name, deleting them..."
		echo "$failed_jobs" | xargs -r kubectl delete -n cms
	fi

	# Check for successful jobs
	local successful_jobs=$(kubectl get jobs -n cms -l "app=conda-env-builder,environment=$env_name,location=$location_label" --field-selector=status.successful=1 -o name 2>/dev/null || echo "")

	# Check for running/pending jobs
	local running_jobs=$(kubectl get jobs -n cms -l "app=conda-env-builder,environment=$env_name,location=$location_label" --field-selector=status.successful!=1,status.failed!=1 -o name 2>/dev/null || echo "")

	# Return status: 0 if job should be created, 1 if job exists
	if [ -z "$running_jobs" ] && [ -z "$successful_jobs" ]; then
		return 0 # Job should be created
	else
		if [ -n "$running_jobs" ]; then
			echo "Skipping ($location_label) for $env_name - job already running or pending"
		else
			echo "Skipping ($location_label) for $env_name - job already completed successfully"
		fi
		return 1 # Job exists, don't create
	fi
}

# Function to create job for a specific location
create_job_for_location() {
	local env_name="$1"
	local env_dir="$2"
	local env_file="$3"
	local pip_uninstall_file="$4"
	local location_label="$5"

	local location_root="${LOCATIONS[$location_label]}"
	if [ -z "$location_root" ]; then
		echo "ERROR: Unknown location label: $location_label"
		return 1
	fi

	job_yaml=$(create_job_yaml_location "$env_name" "$env_dir" "$env_file" "$pip_uninstall_file" "$location_root" "$location_label")
	echo "$job_yaml" | kubectl apply -f -
	if [ $? -eq 0 ]; then
		echo "Successfully created job ($location_label) for environment: $env_name"
		return 0
	else
		echo "Failed to create job ($location_label) for environment: $env_name"
		return 1
	fi
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

			# Process each location defined in LOCATIONS array
			for location_label in "${!LOCATIONS[@]}"; do
				echo "Processing location: $location_label"

				# Check if we need to create a job for this location
				if check_and_manage_jobs "$env_name" "$location_label"; then
					# Create job for this location
					create_job_for_location "$env_name" "$env_dir" "$env_file" "$pip_uninstall_file" "$location_label"
				fi
			done
		else
			echo "No environment.yaml or environment.yml found in $env_dir, skipping..."
		fi
	fi
done

# Clean up
rm -rf /tmp/purdue-af-conda-envs

echo "Conda environment job generation completed"
