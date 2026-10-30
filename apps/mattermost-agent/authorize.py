"""Authorizes the bot's OAuth 2.0 application: logs the service account in once,
in a browser, and encrypts the refresh token it gets into secret.yaml."""

from __future__ import annotations

import http.server
import secrets
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

MATTERMOST = "https://mattermost.web.cern.ch"
PORT = 8765
REDIRECT = f"http://localhost:{PORT}/callback"
SECRET = Path(__file__).with_name("secret.yaml")


def authorize_url(client_id: str, state: str) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "state": state,
        }
    )
    return f"{MATTERMOST}/oauth/authorize?{query}"


def receive_code(state: str, port: int = PORT) -> str:
    """Waits for the browser's redirect and returns its code."""
    found: dict[str, str] = {}

    class Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            query = parse_qs(urlparse(self.path).query)
            if query.get("state") == [state] and "code" in query:
                found["code"] = query["code"][0]
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"Authorized. You can close this page.")
            else:
                self.send_response(400)
                self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    with http.server.HTTPServer(("127.0.0.1", port), Callback) as server:
        while "code" not in found:
            server.handle_request()
    return found["code"]


def exchange(
    client: httpx.Client, client_id: str, client_secret: str, code: str
) -> str:
    """The refresh token that the code buys."""
    response = client.post(
        "/oauth/access_token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    response.raise_for_status()
    token: str = response.json()["refresh_token"]
    return token


def secret_value(key: str) -> str:
    extract = f'["stringData"]["{key}"]'
    command = ["sops", "--decrypt", "--extract", extract, str(SECRET)]
    return subprocess.run(
        command, check=True, capture_output=True, text=True
    ).stdout.strip()


def main() -> None:
    client_id = secret_value("client-id")
    client_secret = secret_value("client-secret")
    state = secrets.token_urlsafe(16)
    print("Open this in a private window and log in as the service account:")
    print(authorize_url(client_id, state))
    code = receive_code(state)
    with httpx.Client(base_url=MATTERMOST, timeout=30.0) as client:
        token = exchange(client, client_id, client_secret, code)
    path = '["stringData"]["refresh-token"]'
    subprocess.run(["sops", "set", str(SECRET), path, f'"{token}"'], check=True)
    print(f"Encrypted the refresh token into {SECRET}.", file=sys.stderr)


if __name__ == "__main__":
    main()
