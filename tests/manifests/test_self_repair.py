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
    """Its own kustomization, because deploy/experimental disables generator
    name hashes and the deploy Job relies on the hash to be re-run."""
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
        "deploy-job.yaml",
        "secret-github.yaml",
    ]
    assert "generatorOptions" not in app
    (generator,) = app["configMapGenerator"]
    assert generator["name"] == "self-repair-workflow"
    assert generator["options"]["annotations"] == {
        "kustomize.toolkit.fluxcd.io/substitute": "disabled"
    }
    files = {Path(f).name for f in generator["files"]}
    assert files == {"self_repair.py", "triage.py", "prompts.py", "config.yaml"}
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


def test_deploy_job_is_forced_and_deploys_the_environment():
    (job,) = docs(APP / "deploy-job.yaml")
    assert job["kind"] == "Job"
    assert (
        job["metadata"]["annotations"]["kustomize.toolkit.fluxcd.io/force"] == "enabled"
    )
    spec = job["spec"]["template"]["spec"]
    assert "ttlSecondsAfterFinished" not in job["spec"], (
        "a TTL would make Flux re-run the deploy every minute"
    )
    assert spec["restartPolicy"] == "OnFailure"
    (container,) = spec["containers"]
    script = "".join(container["args"])
    assert (
        "flyte --config config.yaml deploy -p self-repair -d development self_repair.py env"
        in script
    )
    assert "create project --id self-repair" in script
    assert container["workingDir"] == "/workflow"
    assert spec["volumes"][0]["configMap"]["name"] == "self-repair-workflow"
    assert container["image"].endswith("/purdueaf/self-repair:latest")


def test_task_pods_get_the_github_token_from_the_pod_template():
    (template,) = docs(APP / "podtemplate.yaml")
    assert (
        template["kind"] == "PodTemplate"
        and template["metadata"]["name"] == "self-repair"
    )
    (container,) = template["template"]["spec"]["containers"]
    assert container["name"] == "default"
    env = {e["name"]: e["valueFrom"]["secretKeyRef"] for e in container["env"]}
    assert env["GITHUB_TOKEN"] == {"name": "self-repair-github", "key": "token"}
    assert env["OPENCODE_API_KEY"]["optional"] is True
    assert template["template"]["spec"]["securityContext"]["runAsUser"] == 1000

    workflow = (WORKFLOW / "self_repair.py").read_text()
    assert 'pod_template="self-repair"' in workflow


def test_workflow_targets_the_control_plane_and_is_triggered():
    config = yaml.safe_load((WORKFLOW / "config.yaml").read_text())
    flyte_values = yaml.safe_load((REPO / "apps/flyte/values.yaml").read_text())
    assert config["admin"]["endpoint"].startswith(
        f"dns:///{flyte_values['fullnameOverride']}-http.cms"
    )
    assert config["task"] == {"project": "self-repair", "domain": "development"}

    workflow = (WORKFLOW / "self_repair.py").read_text()
    assert re.search(r'flyte\.Cron\("\*/15 \* \* \* \*"\)', workflow)
    assert 'inputs={"trigger_time": flyte.TriggerTime}' in workflow
    assert "triggers=every_15_minutes" in workflow
    assert 'ignored_inputs=("evidence",)' in workflow, (
        "analyze must be cached on the key alone"
    )
    assert '"git push*": "deny"' in workflow and '"git commit*": "deny"' in workflow


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
        "the publish stage only moves :latest for names listed in resolve"
    )
    status = (REPO / ".github/workflows/component-status.py").read_text()
    assert '"self-repair",' in status
    dockerfile = (REPO / "docker/self-repair/Dockerfile").read_text()
    assert re.search(r'^ARG OPENCODE_VERSION="[0-9.]+"$', dockerfile, re.M), (
        "Renovate matches this exact form"
    )
    assert (REPO / "docker/self-repair/uv.lock").is_file()
