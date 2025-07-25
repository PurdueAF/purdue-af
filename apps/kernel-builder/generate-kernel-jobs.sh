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

# Function to create job YAML for a single environment
create_job_yaml() {
	local env_name="$1"
	local env_dir="$2"

	cat <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: kernel-builder-${env_name}-$(date +%s)
  namespace: cms
  labels:
    app: kernel-builder
    environment: ${env_name}
spec:
  template:
    spec:
      serviceAccountName: kernel-builder
      restartPolicy: Never
      containers:
        - name: kernel-builder
          image: rockylinux:8.9
          command:
            - /bin/bash
            - -c
            - |
              # Copy the build script and execute it for this specific environment
              cp /scripts/build-single-kernel.sh /tmp/build-single-kernel.sh
              chmod +x /tmp/build-single-kernel.sh
              /tmp/build-single-kernel.sh "${env_name}" "${env_dir}"
          resources:
            requests:
              memory: "4Gi"
              cpu: "2"
            limits:
              memory: "8Gi"
              cpu: "4"
          volumeMounts:
            - name: af-shared-storage
              mountPath: /work/
              mountPropagation: HostToContainer
            - name: scripts
              mountPath: /scripts/
              readOnly: true
      volumes:
        - name: af-shared-storage
          persistentVolumeClaim:
            claimName: af-shared-storage
        - name: scripts
          configMap:
            name: kernel-builder-scripts
      nodeSelector:
        cms-af-prod: "true"
      tolerations:
        - key: "hub.jupyter.org/dedicated"
          operator: "Equal"
          value: "cms-af"
          effect: "NoSchedule"
EOF
}

# Find all directories and create jobs for them
echo "Scanning for directories with environment.yaml files..."
for dir in */; do
	if [ -d "$dir" ]; then
		local env_name=$(basename "$dir")
		local env_dir="${dir%/}"

		echo "Processing directory: $env_dir"
		echo "Environment name: $env_name"

		# Validate environment name
		if ! validate_env_name "$env_name"; then
			echo "Skipping invalid environment: $env_name"
			continue
		fi

		# Check if environment.yaml exists
		if [ -f "${env_dir}/environment.yaml" ]; then
			echo "Found environment.yaml in $env_dir, creating job..."

			# Create job YAML
			job_yaml=$(create_job_yaml "$env_name" "$env_dir")

			# Apply the job
			echo "$job_yaml" | kubectl apply -f -

			if [ $? -eq 0 ]; then
				echo "Successfully created job for environment: $env_name"
			else
				echo "Failed to create job for environment: $env_name"
			fi
		else
			echo "No environment.yaml found in $env_dir, skipping..."
		fi
	fi
done

# Clean up old jobs (keep only last 10 jobs per environment)
echo "Cleaning up old jobs..."
for env_name in $(kubectl get jobs -n cms -l app=kernel-builder -o jsonpath='{.items[*].metadata.labels.environment}' | tr ' ' '\n' | sort -u); do
	echo "Cleaning up old jobs for environment: $env_name"
	kubectl get jobs -n cms -l "app=kernel-builder,environment=$env_name" --sort-by=.metadata.creationTimestamp -o name | tail -n +11 | xargs -r kubectl delete -n cms
done

# Clean up
rm -rf /tmp/purdue-af-kernels

echo "Kernel job generation completed"
