"""Mint the NRP interLink plugin's next token with its current one, and write it into
the kubeconfig-nrp-interlink Secret, with a kubeconfig whose user reads that token
from its file: the plugin and the ray-train gateway pick it up without a restart.

    python renew.py    # in the token-renewal CronJob, where the Secret is mounted as the plugin mounts it
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import yaml
from kubernetes import client, config

SECRET = "kubeconfig-nrp-interlink"
# Where the plugin mounts the Secret (../values.yaml), so the exec command finds the token there too.
MOUNT = Path("/interlink-kubernetes-plugin/private/k8s")
NAMESPACE_FILE = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
LIFETIME_S = 30 * 86400
# Prints the ExecCredential of the token in TOKEN_FILE, valid for ten minutes: a client
# runs it again after that, and so reads a renewed token.
CREDENTIAL = (
    "import datetime, json, os\n"
    "token = open(os.environ['TOKEN_FILE']).read().strip()\n"
    "until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=10)\n"
    "print(json.dumps({'apiVersion': 'client.authentication.k8s.io/v1', 'kind': 'ExecCredential',"
    " 'status': {'token': token, 'expirationTimestamp': until.strftime('%Y-%m-%dT%H:%M:%SZ')}}))\n"
)


def renewed(kubeconfig: dict[str, Any], token_file: Path) -> dict[str, Any]:
    """The kubeconfig, its one user reading its token from token_file."""
    (user,) = kubeconfig["users"]
    exec_user = {
        "apiVersion": "client.authentication.k8s.io/v1",
        "interactiveMode": "Never",
        "command": "python",
        "args": ["-c", CREDENTIAL],
        "env": [{"name": "TOKEN_FILE", "value": str(token_file)}],
    }
    return kubeconfig | {"users": [{"name": user["name"], "user": {"exec": exec_user}}]}


def encoded(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def main() -> None:
    path = MOUNT / "kubeconfig.yaml"
    kubeconfig = yaml.safe_load(path.read_text())
    (user,) = kubeconfig["users"]
    (context,) = kubeconfig["contexts"]
    nrp = client.CoreV1Api(config.new_client_from_config(str(path)))
    answer = nrp.create_namespaced_service_account_token(
        user["name"],
        context["context"]["namespace"],
        client.AuthenticationV1TokenRequest(
            spec=client.V1TokenRequestSpec(audiences=[], expiration_seconds=LIFETIME_S)
        ),
    )
    here = client.Configuration()
    config.load_incluster_config(client_configuration=here)
    namespace = NAMESPACE_FILE.read_text().strip()
    client.CoreV1Api(client.ApiClient(here)).patch_namespaced_secret(
        SECRET,
        namespace,
        {
            "data": {
                "kubeconfig.yaml": encoded(
                    yaml.safe_dump(
                        renewed(kubeconfig, MOUNT / "token"), sort_keys=False
                    )
                ),
                "token": encoded(answer.status.token),
            }
        },
    )
    print(f"renewed {user['name']}'s token, until {answer.status.expiration_timestamp}")


if __name__ == "__main__":
    main()
