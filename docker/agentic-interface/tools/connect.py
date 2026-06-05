"""Session connection — SSH key injection for persistent shell access.

connect_to_session injects the agent's public key into the pod's
authorized_keys, checking for an existing entry first (idempotent).
The agent then uses `ssh PurdueAF "cmd"` for all pod operations.
"""

import base64
import os

from context import current_user

_NAMESPACE = os.environ.get("NAMESPACE", "cms")
_CONTAINER = "notebook"
_SSH_HOST = "cms.geddes.rcac.purdue.edu"
_SSH_PORT = 22


def _render_ssh_setup(username: str) -> str:
    """Build the local SSH bootstrap command, username and host baked in.

    Single source of truth for the client-side setup — the agent runs the
    returned string verbatim.  Tries an existing connection first and only does
    key/config setup if that fails.  Uses a quoted heredoc so nothing in the
    config block is shell-expanded; ssh expands the leading ~ in path options.
    """
    return f"""\
if ssh -o BatchMode=yes -o ConnectTimeout=5 PurdueAF "hostname" 2>/dev/null; then
  echo "ALREADY_CONNECTED"
else
  [ -f ~/.ssh/af_key ] \\
    || ssh-keygen -t ed25519 -f ~/.ssh/af_key -N "" -C "purdue-af-agentic" -q
  grep -q "Host PurdueAF" ~/.ssh/config 2>/dev/null \\
    || cat >> ~/.ssh/config << 'EOF'
Host PurdueAF
    HostName {_SSH_HOST}
    Port {_SSH_PORT}
    User {username}
    IdentityFile ~/.ssh/af_key
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    ControlMaster auto
    ControlPath ~/.ssh/control-af-%r@%h:%p
    ControlPersist 60m
EOF
  cat ~/.ssh/af_key.pub
fi"""


async def _check_and_inject(pod_name: str, username: str, public_key: str) -> str:
    """Check if public_key is in authorized_keys; add it if not.

    Verifies pod ownership by label before any exec.
    Returns 'EXISTS', 'ADDED', or an 'Error: ...' string.
    """
    try:
        from kubernetes import client, config
        from kubernetes.stream import stream as k8s_stream
    except ImportError:
        return "Error: service is misconfigured — contact AF support"

    try:
        config.load_incluster_config()
    except Exception:
        return "Error: service is not running in the expected environment — contact AF support"

    # Base64-encode the key so special characters can't break the shell script.
    encoded = base64.b64encode(public_key.strip().encode()).decode()

    script = f"""
set -e
KEY=$(printf '%s' '{encoded}' | base64 -d)

# Ensure directory and file exist with correct permissions.
mkdir -p ~/.ssh
chmod 700 ~/.ssh
touch ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys

# Compare by key-type + key-data only (ignore the comment field) so a
# previously injected key is recognised even if the comment has changed.
KEY_SIG=$(printf '%s' "$KEY" | awk '{{print $1, $2}}')

if grep -qF "$KEY_SIG" ~/.ssh/authorized_keys 2>/dev/null; then
    printf 'EXISTS\\n'
else
    printf '%s\\n' "$KEY" >> ~/.ssh/authorized_keys
    printf 'ADDED\\n'
fi
"""

    v1 = client.CoreV1Api()

    # Verify pod ownership before exec — the RBAC grants namespace-wide exec
    # permission, so this label check is the code-level gate that prevents the
    # service from ever touching another user's pod or a privileged system pod.
    try:
        pod = v1.read_namespaced_pod(name=pod_name, namespace=_NAMESPACE)
        pod_user = (pod.metadata.labels or {}).get("username_unescaped", "")
        if pod_user != username:
            return "Error: pod ownership verification failed — access denied"
    except Exception:
        return "Error: could not verify pod ownership — contact AF support"

    try:
        output = k8s_stream(
            v1.connect_get_namespaced_pod_exec,
            name=pod_name,
            namespace=_NAMESPACE,
            command=["bash", "-c", script],
            container=_CONTAINER,
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
        )
        return output.strip()
    except Exception as exc:
        return f"Error: could not access your session — ensure it is running and try again. ({exc})"


def register(mcp) -> None:
    @mcp.tool()
    async def prepare_ssh_connection() -> str:
        """Generate the local SSH key/config for connecting to the user's AF pod.

        Returns one exact, ready-to-run shell command — your username and the AF
        host are already filled in from your token, so run it verbatim (a single
        Bash call).  It tries an existing connection first and only does key/config
        setup if that fails, so repeat connects need just one approval.

        After running it:
          - output "ALREADY_CONNECTED" -> already connected; stop here.
          - otherwise it prints an SSH public key -> pass it to connect_to_session.

        First step of connecting.  Ask the user before connecting via SSH.
        """
        user = current_user.get()
        username = user["username"]
        pod_name = user["pod_name"]

        setup = _render_ssh_setup(username)
        lines: list[str] = []
        if not pod_name:
            lines += [
                "Note: no running session detected — start one with "
                "start_af_session first, or the connection test will fail.",
                "",
            ]
        lines += [
            "Run this locally (single Bash call):",
            "",
            setup,
            "",
            'If it prints "ALREADY_CONNECTED": done — verify with ssh PurdueAF "hostname".',
            "Otherwise pass the printed public key to connect_to_session(public_key=...).",
        ]
        return "\n".join(lines)

    @mcp.tool()
    async def connect_to_session(public_key: str) -> str:
        """Inject an SSH public key into the user's running AF pod (idempotent).

        Call prepare_ssh_connection first — it generates the local key and prints
        the public key to pass here.  This tool adds that key to the pod's
        ~/.ssh/authorized_keys only if not already present, so it is safe to call
        repeatedly.  After it succeeds, verify with: ssh PurdueAF "hostname".

        Args:
            public_key: The public key printed by prepare_ssh_connection
                        (contents of ~/.ssh/af_key.pub, one line).
        """
        user = current_user.get()
        pod_name = user["pod_name"]
        username = user["username"]

        if not pod_name:
            return (
                "No running session found — call start_af_session first, "
                "then retry connect_to_session once get_session_status shows 'running'."
            )

        result = await _check_and_inject(pod_name, username, public_key)

        if result == "EXISTS":
            key_status = (
                "Key already present in authorized_keys — no changes made to the pod."
            )
        elif result == "ADDED":
            key_status = "Key added to pod's authorized_keys."
        else:
            return result  # propagate error string

        return "\n".join(
            [
                key_status,
                f"Pod: {pod_name}  User: {username}",
                "",
                'Ready. Run: ssh PurdueAF "hostname"',
                "If that fails (stale socket after restart): rm -f ~/.ssh/control-af-*",
            ]
        )
