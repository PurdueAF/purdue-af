import argparse
import subprocess

import yaml


def load_secrets_from_file(file_path):
    with open(file_path, "r") as file:
        secrets = yaml.safe_load(file)
    return secrets


def generate_secrets_yaml(secrets, namespace):
    output_yaml = ""
    for name, data in secrets.items():
        secret_yaml = f"""
---
apiVersion: v1
kind: Secret
metadata:
  namespace: {namespace}
  name: {name}
type: Opaque
stringData:
"""
        for key, value in data.items():
            secret_yaml += f"  {key}: {value}\n"
        output_yaml += secret_yaml

    return output_yaml


def apply_secrets(secrets_yaml, namespace):
    try:
        subprocess.run(
            ["kubectl", "apply", "-n", namespace, "-f", "-"],
            input=secrets_yaml,
            text=True,
            check=True,
        )
        print("Secrets applied successfully.")
    except subprocess.CalledProcessError as e:
        print(f"Error applying secrets: {e}")


def main():
    parser = argparse.ArgumentParser(description="Generate and apply secrets YAML.")
    parser.add_argument(
        "-n",
        "--namespace",
        default="cms",
        choices=["cms", "cms-dev"],
        help="Namespace for the secrets (default: cms)",
    )

    args = parser.parse_args()

    if args.namespace == "cms":
        secrets = load_secrets_from_file("secrets.yaml")
    elif args.namespace == "cms-dev":
        secrets = load_secrets_from_file("secrets-dev.yaml")
    else:
        print(f"Invalid namespace: {args.namespace}")
        return

    secrets_yaml = generate_secrets_yaml(secrets, args.namespace)
    apply_secrets(secrets_yaml, args.namespace)


if __name__ == "__main__":
    main()
