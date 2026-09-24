"""Every Secret committed under apps/ is SOPS-encrypted to the key the Flux roots
decrypt with. Chart templates render theirs from values and are skipped."""

import yaml
from common import REPO

APPS = REPO / "apps"
SOPS_RULES = yaml.safe_load((REPO / ".sops.yaml").read_text())["creation_rules"]
RECIPIENT = SOPS_RULES[0]["age"]


def committed_secrets():
    for path in sorted(APPS.rglob("*.y*ml")):
        if "templates" in path.relative_to(APPS).parts:
            continue
        text = path.read_text()
        if "kind: Secret" not in text:
            continue
        for doc in yaml.safe_load_all(text):
            if isinstance(doc, dict) and doc.get("kind") == "Secret":
                yield path.relative_to(REPO), doc


def test_secrets_are_found():
    assert any(True for _ in committed_secrets())


def test_every_committed_secret_is_sops_encrypted():
    for path, secret in committed_secrets():
        sops = secret.get("sops")
        assert sops and sops.get("mac"), f"{path}: no sops block"
        assert RECIPIENT in [r["recipient"] for r in sops["age"]], (
            f"{path}: not encrypted to the .sops.yaml key"
        )
        # Key names only: the failure message must not echo a secret value.
        plaintext = [
            f"{field}.{key}"
            for field in ("data", "stringData")
            for key, value in (secret.get(field) or {}).items()
            if value and not str(value).startswith("ENC[")
        ]
        assert not plaintext, f"{path}: plaintext {plaintext}"
