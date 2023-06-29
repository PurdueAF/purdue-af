import os, subprocess, json

# User-installed kernels from conda environments
env_list_output = subprocess.run(["conda", "env", "list"], capture_output=True, text=True).stdout
env_list = env_list_output.strip().split('\n')[2:]

preinstalled_envs = {
    "python3": {
        "path": "/depot/cms/kernels/python3",
        "display-name": "Python3 kernel (default)"
    },
    "python3-ml": {
        "path": "/depot/cms/kernels/python3-ml",
        "display-name": "Python3 kernel [ML]"
    }
}
preinstalled_envs_paths = [p["path"] for p in preinstalled_envs.values()]

env_list = [env.split()[-1] for env in env_list] + preinstalled_envs_paths
print(env_list)

def make_kernel(env_path):
    env = os.environ.copy()

    if ('#' in env_path) or (env_path == "/opt/conda"):
        return

    env_name = env_path.split('/')[-1].lower()

    # subprocess.run(f"conda activate {env_path}", shell=True, executable="/bin/bash")
    cmd = f"conda run -p {env_path} ipython kernel install --prefix=/opt/conda --name={env_name}"
    if env_name in preinstalled_envs:
        disp_name = preinstalled_envs[env_name]["display-name"]
        cmd += f' --display-name "{disp_name}"'
    try:
        subprocess.run(cmd, shell=True, executable="/bin/bash")
    except:
        pass
    if env_name not in preinstalled_envs:
        kernel_path = "/opt/conda/share/jupyter/kernels/"
        wrapper_path = f"{kernel_path}{env_name}/wrapper.sh"
        with open(wrapper_path, 'w') as wrapper_file:
            wrapper_file.write(
                f'''#!/bin/bash
eval "$(command conda shell.bash hook 2> /dev/null)"
conda activate {env_path}
exec {env_path}/bin/python "$@"
'''
            )
        subprocess.run(f"chmod 777 {kernel_path}{env_name}/*", shell=True, executable="/bin/bash")
        with open(f"{kernel_path}{env_name}/kernel.json", 'r+') as kernel_json_file:
            kernel_json = json.load(kernel_json_file)
            kernel_json['argv'][0] = wrapper_path
            kernel_json_file.seek(0)
            json.dump(kernel_json, kernel_json_file, indent=2)
            kernel_json_file.truncate()
    # subprocess.run("conda deactivate", shell=True, executable="/bin/bash")

use_dask = True

if use_dask:
    import dask
    delayed_commands = [dask.delayed(make_kernel)(env) for env in env_list]
    results = dask.compute(*delayed_commands, scheduler='threads')
else:
    for env in env_list:
        make_kernel(env)

# for env_path in env_list:
#     if "#" not in env_path and env_path != "/opt/conda":
#         subprocess.run(["conda", "activate", env_path])
#         env_name = env_path.split('/')[-1].lower()
#         print(env_name)
#         subprocess.run(["ipython", "kernel", "install", "--prefix=/opt/conda", f"--name={env_name}"])
#         kernel_path = "/opt/conda/share/jupyter/kernels/"
#         wrapper_path = f"{kernel_path}{env_name}/wrapper.sh"
#         with open(wrapper_path, 'w') as wrapper_file:
#             wrapper_file.write(
#                 f'''
#                 #!/bin/bash
#                 eval "$(command conda shell.bash hook 2> /dev/null)"
#                 conda activate {env_path}
#                 exec {env_path}/bin/python "$@"
#                 '''
#             )
#         subprocess.run(["chmod", "777", f"{kernel_path}{env_name}/*"])
#         with open(f"{kernel_path}{env_name}/kernel.json", 'r+') as kernel_json_file:
#             kernel_json = json.load(kernel_json_file)
#             kernel_json['argv'][0] = wrapper_path
#             kernel_json_file.seek(0)
#             json.dump(kernel_json, kernel_json_file, indent=2)
#             kernel_json_file.truncate()
#         subprocess.run(["conda", "deactivate"])

# # Pre-installed kernels
# subprocess.run(["conda", "activate", "/depot/cms/kernels/python3"])
# subprocess.run(["ipython", "kernel", "install", "--prefix=/opt/conda", "--name=python3", "--display-name", "Python3 kernel (default)"])
# subprocess.run(["conda", "deactivate"])
# subprocess.run(["conda", "activate", "/depot/cms/kernels/python3-ml"])
# subprocess.run(["ipython", "kernel", "install", "--prefix=/opt/conda", "--name=python3-ml", "--display-name", "Python3 kernel [ML]"])
# subprocess.run(["conda", "deactivate"])

subprocess.run(["jupyter", "kernelspec", "list"])
