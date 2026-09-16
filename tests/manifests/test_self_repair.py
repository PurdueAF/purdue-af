"""Wiring of apps/self-repair, workflows/self-repair and docker/self-repair."""

import re
from pathlib import Path

import yaml
from common import REPO

APP = REPO / "apps" / "self-repair"
WORKFLOW = REPO / "workflows" / "self-repair"
EXPERIMENTAL = REPO / "deploy" / "experimental" / "kustomization.yaml"
CI_IMAGES = REPO / ".github" / "workflows" / "ci-images.yml"
IMAGE_INPUTS = REPO / ".github" / "workflows" / "image-inputs.sh"


def docs(path: Path):
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


def test_flux_deploys_the_app_as_one_kustomization():
    experimental = yaml.safe_load(EXPERIMENTAL.read_text())
    assert "../../apps/self-repair" in experimental["resources"]
    assert not any(
        "self-repair" in r
        for r in experimental["resources"]
        if r != "../../apps/self-repair"
    )
    assert not any(
        "self-repair" in g["name"] for g in experimental["configMapGenerator"]
    )

    app = yaml.safe_load((APP / "kustomization.yaml").read_text())
    assert app["resources"] == [
        "podtemplate.yaml",
        "cronjob.yaml",
        "secret-github.yaml",
        "secret-genai.yaml",
    ]
    (generator,) = app["configMapGenerator"]
    assert generator["name"] == "self-repair-workflow"
    assert generator["options"]["annotations"] == {
        "kustomize.toolkit.fluxcd.io/substitute": "disabled"
    }
    files = {Path(f).name for f in generator["files"]}
    assert files == {
        "self_repair.py",
        "triage.py",
        "prompts.py",
        "genai_proxy.py",
        "config.yaml",
    }
    for f in generator["files"]:
        assert (APP / f).resolve().is_file(), f


def test_github_token_secret_is_encrypted():
    """A plaintext token here would be public the moment it is pushed."""
    raw = (APP / "secret-github.yaml").read_text()
    assert "github_pat_" not in raw and "ghp_" not in raw
    (secret,) = docs(APP / "secret-github.yaml")
    assert secret["kind"] == "Secret"
    assert secret["metadata"]["name"] == "self-repair-github"
    assert secret["stringData"]["token"].startswith("ENC[AES256_GCM,")
    assert secret["sops"]["encrypted_regex"] == "^(data|stringData)$"
    assert any(r["recipient"].startswith("age1") for r in secret["sops"]["age"])


def test_genai_key_secret_is_encrypted():
    raw = (APP / "secret-genai.yaml").read_text()
    assert "sk-" not in raw
    (secret,) = docs(APP / "secret-genai.yaml")
    assert secret["metadata"]["name"] == "self-repair-genai"
    assert secret["stringData"]["api-key"].startswith("ENC[AES256_GCM,")
    assert any(r["recipient"].startswith("age1") for r in secret["sops"]["age"])


def test_launcher_is_a_suspended_cronjob_running_the_module():
    (cronjob,) = docs(APP / "cronjob.yaml")
    assert cronjob["kind"] == "CronJob" and cronjob["metadata"]["name"] == "self-repair"
    spec = cronjob["spec"]
    assert spec["suspend"] is True, "testing: ticks are started by hand"
    assert spec["concurrencyPolicy"] == "Forbid"
    pod = spec["jobTemplate"]["spec"]["template"]["spec"]
    (container,) = pod["containers"]
    script = "".join(container["args"])
    assert "cp -L /workflow/*.py" in script, (
        "ConfigMap files are symlinks; the bundler skips them"
    )
    assert script.endswith("python self_repair.py")
    assert container["image"].endswith("/purdueaf/self-repair:latest")
    assert pod["volumes"][0]["configMap"]["name"] == "self-repair-workflow"
    assert pod["securityContext"]["runAsUser"] == 1000
    # the manual trigger documented in the README must match the CronJob name
    assert "create job --from=cronjob/self-repair" in (APP / "README.md").read_text()


def test_task_pods_get_the_github_token_from_the_pod_template():
    (template,) = docs(APP / "podtemplate.yaml")
    assert template["kind"] == "PodTemplate"
    assert template["metadata"]["name"] == "self-repair"
    (container,) = template["template"]["spec"]["containers"]
    assert container["name"] == "default"
    env = {e["name"]: e["valueFrom"]["secretKeyRef"] for e in container["env"]}
    assert env["GITHUB_TOKEN"] == {"name": "self-repair-github", "key": "token"}
    assert env["OPENCODE_API_KEY"]["optional"] is True
    assert env["GENAI_API_KEY"] == {"name": "self-repair-genai", "key": "api-key"}
    assert template["template"]["spec"]["securityContext"]["runAsUser"] == 1000

    workflow = (WORKFLOW / "self_repair.py").read_text()
    assert 'pod_template="self-repair"' in workflow


def test_workflow_names_its_runs_and_needs_no_trigger():
    config = yaml.safe_load((WORKFLOW / "config.yaml").read_text())
    flyte_values = yaml.safe_load((REPO / "apps/flyte/values.yaml").read_text())
    assert config["admin"]["endpoint"].startswith(
        f"dns:///{flyte_values['fullnameOverride']}-http.cms"
    )
    assert config["task"] == {"project": "self-repair", "domain": "development"}

    workflow = (WORKFLOW / "self_repair.py").read_text()
    assert "flyte.Trigger(" not in workflow and "triggers=" not in workflow
    for call in (
        'run_name("triage", tick_of(now))',
        'run_name("watch", tick)',
        'run_name("analyze", tick, incident.key.fingerprint)',
        'run_name("fix", tick, fingerprint)',
    ):
        assert call in workflow, call
    assert "flyte.with_runcontext(name=" in workflow
    assert 'ignored_inputs=("evidence",)' in workflow, (
        "analyze is cached on the key alone"
    )
    assert "analyze_within_budget(" in workflow, "the budget counts fresh analyses only"
    assert '"git push*": "deny"' in workflow and '"git commit*": "deny"' in workflow
    assert "webfetch" not in workflow and "websearch" not in workflow, (
        "the agent keeps the web; the prompt and the timeout keep it on time"
    )
    assert "AGENT_BUDGET_MINUTES" in workflow
    assert 'os.environ.get("SELF_REPAIR_MODEL", "genai/gpt-oss:120b")' in workflow
    assert "from genai_proxy import Proxy" in workflow, (
        "opencode must not talk to GenAI Studio directly"
    )
    assert 'providers["genai"]["options"]["baseURL"] = f"{proxy.url}/api"' in workflow
    assert '"apiKey": "{env:GENAI_API_KEY}"' in workflow
    assert "CatalogCacheStatus.Name(" in workflow, "cache hits are logged"
    assert "@env.task(report=True" in workflow, "the verdict table is the triage report"
    assert "flyte.report.replace.aio(" in workflow
    assert '"*docker/dask-gateway-server/*": "deny"' in workflow, (
        "the vendored fork is off limits"
    )
    assert "_python_defects(repo, paths)" in workflow, "no PR without a pyflakes pass"
    assert "silences(_git(" in workflow, "a log-level change is not a fix"
    assert "context=_context(evidence, key.container)" in workflow, (
        "the agent sees the surrounding lines"
    )
    assert 'run_name("dedupe", tick)' in workflow, "one grouping call per tick"
    assert "class _Watch" in workflow and "PROVIDER_GRACE_S" in workflow, (
        "a provider error followed by silence must not wait for the hard timeout"
    )


def test_image_is_built_published_and_pinned_consistently():
    inputs = IMAGE_INPUTS.read_text()
    assert "\tself-repair)" in inputs and "docker/self-repair" in inputs
    ci = yaml.safe_load(CI_IMAGES.read_text())
    matrix = {
        m["name"]: m
        for m in ci["jobs"]["build-aux-images"]["strategy"]["matrix"]["include"]
    }
    assert matrix["self-repair"]["dockerfile"] == "docker/self-repair/Dockerfile"
    assert "opencode --version" in matrix["self-repair"]["smoke"]
    resolve = ci["jobs"]["resolve"]["steps"][-1]["run"]
    assert "self-repair" in resolve, (
        "publish only moves :latest for names listed in resolve"
    )
    status = (REPO / ".github/workflows/component-status.py").read_text()
    assert '"self-repair",' in status
    dockerfile = (REPO / "docker/self-repair/Dockerfile").read_text()
    assert re.search(r'^ARG OPENCODE_VERSION="[0-9.]+"$', dockerfile, re.M), (
        "Renovate matches this exact form"
    )
    assert (REPO / "docker/self-repair/uv.lock").is_file()
