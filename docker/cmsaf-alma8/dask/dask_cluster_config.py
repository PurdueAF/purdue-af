import argparse
import os
import sys

import ruamel.yaml
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

yaml = ruamel.yaml.YAML(typ="rt")
yaml.preserve_quotes = True
yaml.explicit_start = True

HOME = os.environ["HOME"]
CONFIG_FILE = f"{HOME}/.config/dask/labextension.yaml"


def load_config():
    with open(CONFIG_FILE, "r") as file:
        lines = file.readlines()

    # Uncomment the lines
    for i in range(len(lines)):
        if lines[i].startswith("#"):
            lines[i] = lines[i][1:]

    config = yaml.load("".join(lines))
    return config


def save_config(config):
    with open(CONFIG_FILE, "w") as file:
        yaml.dump(config, file)


def set_config_value(config, key, value):
    keys = key.split(".")
    current = config
    for k in keys[:-1]:
        current = current.setdefault(k, {})
    if isinstance(value, str):
        current[keys[-1]] = DoubleQuotedScalarString(value)
    else:
        current[keys[-1]] = value


def remove_config_value(config, key):
    keys = key.split(".")
    current = config
    for k in keys[:-1]:
        if k not in current:
            return
        current = current[k]
    current.pop(keys[-1], None)


def set_mode_slurm(config):
    set_config_value(config, "labextension.factory.module", "purdue_slurm")
    set_config_value(config, "labextension.factory.class", "PurdueSLURMCluster")
    set_config_value(config, "labextension.factory.kwargs.account", "cms")
    set_config_value(config, "labextension.factory.kwargs.cores", 1)
    set_config_value(config, "labextension.factory.kwargs.memory", "2G")
    set_config_value(
        config,
        "labextension.factory.kwargs.job_extra_directives",
        [
            "--qos=normal",
            "-o /tmp/dask_job.%j.%N.out",
            "-e /tmp/dask_job.%j.%N.error",
        ],
    )
    if "python" not in config["labextension"]["factory"]["kwargs"].keys():
        set_config_value(
            config,
            "labextension.factory.kwargs.python",
            "/depot/cms/kernels/python3/bin/python3",
        )


def reset_mode(config):
    set_config_value(config, "labextension.factory.module", "dask.distributed")
    set_config_value(config, "labextension.factory.class", "LocalCluster")
    remove_config_value(config, "labextension.factory.kwargs.account")
    remove_config_value(config, "labextension.factory.kwargs.cores")
    remove_config_value(config, "labextension.factory.kwargs.memory")
    remove_config_value(config, "labextension.factory.kwargs.job_extra_directives")
    remove_config_value(config, "labextension.factory.kwargs.python")


def set_python(config, python_path):
    set_config_value(config, "labextension.factory.kwargs.python", python_path)


def reset_python(config):
    if config["labextension"]["factory"]["class"] == "LocalCluster":
        remove_config_value(config, "labextension.factory.kwargs.python")
    else:
        set_config_value(
            config,
            "labextension.factory.kwargs.python",
            "/depot/cms/kernels/python3/bin/python3",
        )


def set_workers(config, workers):
    set_config_value(config, "labextension.default.workers", workers)
    remove_config_value(config, "labextension.default.adapt")


def set_adapt(config, min_adapt, max_adapt):
    set_config_value(config, "labextension.default.adapt.minimum", min_adapt)
    set_config_value(config, "labextension.default.adapt.maximum", max_adapt)
    remove_config_value(config, "labextension.default.workers")


def main():
    parser = argparse.ArgumentParser(description="Dask cluster configuration manager")
    parser.add_argument(
        "-m",
        "--mode",
        choices=["slurm", "local", "reset"],
        help="Set the cluster mode (local or SLURM).\n"
        "Passing '-m reset' option is equivalent to selecting LocalCluster.",
    )
    parser.add_argument(
        "-p",
        "--python",
        metavar="PYTHON_PATH",
        help="Set the Python executable path available to workers - works only with SLURMCluster\n"
        "(default: /depot/cms/kernels/python3/bin/python3.10)\n"
        "Passing '-p reset' will restore the value to the default one.",
    )
    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        help="Set the default number of workers spawned when a new cluster is created",
    )
    parser.add_argument(
        "-a",
        "--adapt",
        nargs=2,
        type=int,
        metavar=("MIN", "MAX"),
        help="Set the range for number of workers for adaptive cluster size (mutually exclusive with -w argument) ",
    )
    parser.add_argument(
        "-r",
        "--reset",
        action="store_true",
        help="Reset the configuration to default values.",
    )
    parser.add_argument(
        "-d",
        "--display",
        action="store_true",
        help="Display full contents of the config (~/.config/dask/labextension.yaml)",
    )
    args = parser.parse_args()

    print()
    print()
    print("--------------------- Configuring Dask Labextension ---------------------")
    print()
    config = load_config()

    if args.display:
        print(" > Current configuration of Dask Labextension:")
        print()
        yaml.dump(config, sys.stdout)
        return

    if args.reset:
        print(
            " > Resetting Dask Labextension configuration to LocalCluster with 1 worker."
        )
        reset_mode(config)
        reset_python(config)
        set_config_value(config, "labextension.default.workers", 1)
        remove_config_value(config, "labextension.default.adapt")

    if args.mode:
        if args.mode == "slurm":
            print(" > Switching to PurdueSLURMCluster()")
            set_mode_slurm(config)
        elif (args.mode == "local") or (args.mode == "reset"):
            print(" > Switching to LocalCluster()")
            reset_mode(config)

    if args.python:
        if args.python == "reset":
            if config["labextension"]["factory"]["class"] == "LocalCluster":
                print(
                    " > Deleting python executable, since the cluster type is LocalCluster."
                )
            else:
                print(" > Resetting python executable to default for SLURM cluster:")
                print("     /depot/cms/kernels/python3/bin/python3")
            reset_python(config)
        else:
            if config["labextension"]["factory"]["class"] == "LocalCluster":
                print(" > Can't set python executable for LocalCluster.")
                print(" > Run with argument '-m slurm' to switch to SLURM cluster.")
            else:
                print(" > Switching to the following python executable:")
                print(f"    > {args.python}")
                set_python(config, args.python)

    if args.workers and args.adapt:
        print(" > Please choose either --workers or --adapt, but not both.")
        return
    elif args.workers:
        print(f" > Setting default number of workers to {args.workers}.")
        print(" > Adaptive mode will be disabled by default")
        print(" > (this can be manually changed in the extension interface)")
        set_workers(config, args.workers)
    elif args.adapt:
        min_adapt, max_adapt = args.adapt
        print(f" > Setting adaptive mode with")
        print(f"     minimum number of workers: {min_adapt} ")
        print(f"     maximum number of workers: {max_adapt} ")
        print(" > (this can be manually changed in the extension interface)")
        set_adapt(config, min_adapt, max_adapt)

    save_config(config)


if __name__ == "__main__":
    main()
    print()
    print(" > Please restart the pod to apply changes, then use [+ New] button")
    print(" > in Dask Labextension to create a cluster with updated configuration.")

    print("--------------------------------- Done! ---------------------------------")
    print()
