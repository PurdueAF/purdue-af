eval "$(command conda shell.bash hook 2>/dev/null)"
conda init bash
export CONDARC=/home/$NB_USER/.condarc
echo CONDARC=$CONDARC

jupyter kernelspec remove -y python3

conda run -p /depot/cms/kernels/python3 ipython kernel install \
	--prefix=/opt/conda --name="python3" --display-name "Python3 kernel (default)"

kernel_path="/opt/conda/share/jupyter/kernels/python3/"
LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/depot/cms/purdue-af/lhapdf/lib:/depot/cms/purdue-af/combine/HiggsAnalysis/CombinedLimit/build/lib
PYTHONPATH=$PYTHONPATH:/depot/cms/purdue-af/lhapdf/lib/python3.10/site-packages:/depot/cms/purdue-af/combine/HiggsAnalysis/CombinedLimit/build/lib/python
PATH=/depot/cms/kernels/python3/bin/:/depot/cms/purdue-af/combine/HiggsAnalysis/CombinedLimit/build/bin:$PATH:/depot/cms/purdue-af/lhapdf/bin
CPLUS_INCLUDE_PATH=/depot/cms/kernels/python3/x86_64-conda-linux-gnu/sysroot/usr/include
export CPLUS_INCLUDE_PATH=$CPLUS_INCLUDE_PATH

LD_LIBRARY_PATH="$LD_LIBRARY_PATH" \
	PYTHONPATH="$PYTHONPATH" \
	PATH="$PATH" \
	CPLUS_INCLUDE_PATH="$CPLUS_INCLUDE_PATH" \
	jq '.env = {"PATH": env.PATH, "PYTHONPATH": env.PYTHONPATH, "LD_LIBRARY_PATH": env.LD_LIBRARY_PATH, "CPLUS_INCLUDE_PATH": env.CPLUS_INCLUDE_PATH}' \
	"$kernel_path/kernel.json" >tmp_kernel.json
mv tmp_kernel.json "$kernel_path/kernel.json"

# For HATS 2024 workshop
# conda run -p /depot/cms/kernels/hats2024 ipython kernel install \
#     --prefix=/opt/conda --name="hats2024"  --display-name "HATS 2024"

# For Coffea_latest
conda run -p /depot/cms/kernels/coffea_latest ipython kernel install \
	--prefix=/opt/conda --name="coffea_latest" --display-name "coffea_latest"

# kernel_path="/opt/conda/share/jupyter/kernels/hats2024/"
# "$kernel_path/kernel.json" > tmp_kernel.json

# ls -ltr $kernel_path

# mv tmp_kernel.json "$kernel_path/kernel.json"

# ------------------

# Define LCG stacks statically
declare -A LCG_STACKS
# Format: [LCG_VERSION]="path display_name"
LCG_STACKS["106b"]="/cvmfs/sft.cern.ch/lcg/views/LCG_106b/x86_64-el8-gcc11-opt/setup.sh LCG_106b"
LCG_STACKS["106b_cuda"]="/cvmfs/sft.cern.ch/lcg/views/LCG_106b_cuda/x86_64-el8-gcc11-opt/setup.sh LCG_106b_cuda"
# LCG_STACKS["107"]="/cvmfs/sft.cern.ch/lcg/views/LCG_107/x86_64-el8-gcc11-opt/setup.sh LCG_107"
# LCG_STACKS["107_cuda"]="/cvmfs/sft.cern.ch/lcg/views/LCG_107_cuda/x86_64-el8-gcc11-opt/setup.sh LCG_107_cuda"

# Loop through each LCG stack and set up kernels
for lcg_version in "${!LCG_STACKS[@]}"; do
	# Split the value into path and display name
	lcg_config=(${LCG_STACKS[$lcg_version]})
	lcg_path=${lcg_config[0]}
	lcg_display_name=${lcg_config[1]}

	echo "Setting up LCG kernel for version $lcg_version: $lcg_display_name..."

	# Save entire environment to a temporary file
	env >/tmp/original_env_${lcg_version}

	# Check if the LCG path exists
	if [ -f "$lcg_path" ]; then
		# Source LCG setup
		source "$lcg_path"

		# Create kernel name
		kernel_name="lcg_${lcg_version}"

		# Install the kernel
		python -m ipykernel install --name "$kernel_name" --display-name "$lcg_display_name"

		kernel_path="/usr/local/share/jupyter/kernels/$kernel_name/"
		LD_LIBRARY_PATH="$LD_LIBRARY_PATH" \
			PYTHONPATH="$PYTHONPATH" \
			PATH="$PATH" \
			CPLUS_INCLUDE_PATH="$CPLUS_INCLUDE_PATH" \
			jq '.env = {"PATH": env.PATH, "PYTHONPATH": env.PYTHONPATH, "LD_LIBRARY_PATH": env.LD_LIBRARY_PATH, "CPLUS_INCLUDE_PATH": env.CPLUS_INCLUDE_PATH}' \
			"$kernel_path/kernel.json" >tmp_kernel.json
		mv tmp_kernel.json "$kernel_path/kernel.json"

		echo "LCG kernel setup complete for $lcg_display_name."

		# Restore environment variables that were modified by LCG
		while IFS='=' read -r key value; do
			if [ -n "$key" ]; then # Skip empty lines
				current_value="${!key}"
				if [[ "$current_value" == *"lcg"* ]]; then                  # If current value contains "lcg"
					if grep -q "^$key=" /tmp/original_env_${lcg_version}; then # If we have original value
						original_value=$(grep "^$key=" /tmp/original_env_${lcg_version} | cut -d'=' -f2-)
						export "$key=$original_value"
					else # If variable didn't exist before
						unset "$key"
					fi
				fi
			fi
		done < <(env)

		# Clean up
		rm /tmp/original_env_${lcg_version}
	else
		echo "Warning: LCG path $lcg_path does not exist. Skipping."
	fi
done

jupyter kernelspec list
