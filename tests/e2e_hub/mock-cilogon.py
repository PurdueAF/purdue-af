"""Minimal CILogon stand-in implementing the OAuth2 authorization-code flow.

stdlib only (runs in python:slim from a ConfigMap). The production
PurdueCILogonOAuthenticator runs unmodified against this: the test overlay
points oauthenticator's authorize_url/token_url/userinfo_url here.

Identity control: tests POST the *next* login's claims to /_identity;
/authorize then mints a code bound to those claims, /oauth2/token exchanges
it for a bearer, /oauth2/userinfo returns the claims. One extra endpoint,
everything else is the standard dance.
"""

import json
import secrets
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

# code/token -> claims; _next holds claims for the next /authorize hit
STATE = {
    "_next": {
        "eppn": "alice@purdue.edu",
        "idp": "https://idp.purdue.edu/idp/shibboleth",
    }
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quiet
        pass

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()

        if self.path == "/_identity":
            STATE["_next"] = json.loads(body)
            self._json(200, {"ok": True})
        elif self.path.startswith("/oauth2/token"):
            params = urllib.parse.parse_qs(body)
            code = params.get("code", [""])[0]
            claims = STATE.pop(code, None)
            if claims is None:
                self._json(400, {"error": "invalid_grant"})
                return
            token = secrets.token_hex(16)
            STATE[token] = claims
            self._json(
                200,
                {"access_token": token, "token_type": "Bearer", "expires_in": 3600},
            )
        else:
            self._json(404, {"error": "not_found"})

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)

        if url.path == "/authorize":
            code = secrets.token_hex(8)
            STATE[code] = dict(STATE["_next"])
            redirect = query["redirect_uri"][0]
            sep = "&" if "?" in redirect else "?"
            location = f"{redirect}{sep}code={code}&state={query.get('state', [''])[0]}"
            self.send_response(302)
            self.send_header("Location", location)
            self.end_headers()
        elif url.path.startswith("/oauth2/userinfo"):
            auth = self.headers.get("Authorization", "")
            token = auth.removeprefix("Bearer ").strip()
            claims = STATE.get(token)
            if claims is None:
                self._json(401, {"error": "invalid_token"})
                return
            self._json(200, claims)
        elif url.path == "/health":
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not_found"})


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 9090), Handler).serve_forever()
