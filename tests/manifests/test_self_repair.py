"""Wiring of apps/self-repair, workflows/self-repair and docker/self-repair."""

import json
import re
from pathlib import Path

import yaml
from common import REPO, load_script

APP = REPO / "apps" / "self-repair"
WORKFLOW = REPO / "workflows" / "self-repair"
EXPERIMENTAL = REPO / "deploy" / "experimental" / "kustomization.yaml"
CI_IMAGES = REPO / ".github" / "workflows" / "ci-images.yml"
IMAGE_INPUTS = REPO / ".github" / "workflows" / "image-inputs.sh"


def docs(path: Path):
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


def test_flux_lists_the_app_file_by_file_like_every_component():
    experimental = yaml.safe_load(EXPERIMENTAL.read_text())
    assert not (APP / "kustomization.yaml").exists()
    listed = {r for r in experimental["resources"] if "self-repair" in r}
    assert listed == {
        f"../../apps/self-repair/{f}"
        for f in (
            "podtemplate.yaml",
            "cronjob.yaml",
            "secret-github.yaml",
            "secret-genai.yaml",
        )
    }
    generators = {
        g["name"]: g
        for g in experimental["configMapGenerator"]
        if g["name"].startswith("self-repair-")
    }
    assert set(generators) == {"self-repair-workflow", "self-repair-platform-context"}
    for generator in generators.values():
        assert generator["options"]["annotations"] == {
            "kustomize.toolkit.fluxcd.io/substitute": "disabled"
        }
        for f in generator["files"]:
            assert (EXPERIMENTAL.parent / f).resolve().is_file(), f
    generator = generators["self-repair-workflow"]
    files = {Path(f).name for f in generator["files"]}
    assert files == {
        "self_repair.py",
        "triage.py",
        "prompts.py",
        "genai_proxy.py",
        "config.yaml",
    }


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


def test_launcher_is_an_hourly_cronjob_running_the_module():
    (cronjob,) = docs(APP / "cronjob.yaml")
    assert cronjob["kind"] == "CronJob" and cronjob["metadata"]["name"] == "self-repair"
    spec = cronjob["spec"]
    assert spec["suspend"] is False
    assert spec["schedule"] == "0 * * * *"
    assert spec["concurrencyPolicy"] == "Forbid"
    # every tick must reach back past the previous one
    workflow = (WORKFLOW / "self_repair.py").read_text()
    window = int(re.search(r"window_minutes: int = (\d+)", workflow).group(1))
    assert window > 60 + spec["startingDeadlineSeconds"] / 60
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


def test_agents_read_the_platform_context_the_sessions_read():
    """One file, docker/purdue-af/agents/platform-context.md, reaches the
    self-repair agents the way it reaches a session's agents: at the path the
    purdue-af image bakes it into, named in opencode's `instructions`."""
    source = "docker/purdue-af/agents/platform-context.md"
    path = "/opt/purdue-af/agents/platform-context.md"
    dockerfile = (REPO / "docker/purdue-af/Dockerfile").read_text()
    assert re.search(
        rf"COPY[^\n]*{re.escape(source)}\s*\\?\s*\n?\s*{re.escape(path)}", dockerfile
    )
    hook = (REPO / "docker/purdue-af/scripts/config-agents.sh").read_text()
    assert f'AGENT_SECTION="{path}"' in hook
    assert 'instructions\\": [\\"${AGENT_SECTION}' in hook

    kustomization = yaml.safe_load(EXPERIMENTAL.read_text())
    (generator,) = [
        g
        for g in kustomization["configMapGenerator"]
        if g["name"] == "self-repair-platform-context"
    ]
    assert generator["files"] == [f"../../{source}"]

    (template,) = docs(APP / "podtemplate.yaml")
    pod = template["template"]["spec"]
    (container,) = pod["containers"]
    (mount,) = [m for m in container["volumeMounts"] if m["name"] == "platform-context"]
    assert mount["mountPath"] == str(Path(path).parent) and mount["readOnly"]
    (volume,) = [v for v in pod["volumes"] if v["name"] == "platform-context"]
    assert volume["configMap"]["name"] == "self-repair-platform-context"

    workflow = (WORKFLOW / "self_repair.py").read_text()
    assert f'PLATFORM_CONTEXT = Path("{path}")' in workflow
    assert 'config["instructions"] = [str(PLATFORM_CONTEXT)]' in workflow


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
    assert 'ignored_inputs=("evidence", "model")' in workflow, (
        "analyze is cached on the key alone"
    )
    assert "analyze_within_budget(" in workflow, "the budget counts fresh analyses only"
    assert '"git push*": "deny"' in workflow and '"git commit*": "deny"' in workflow
    assert "webfetch" not in workflow and "websearch" not in workflow, (
        "the agent keeps the web; the prompt and the timeout keep it on time"
    )
    assert "AGENT_BUDGET_MINUTES" in workflow
    defaults = re.search(r'"SELF_REPAIR_MODELS",\s*"([^"]+)"', workflow).group(1)
    assert defaults.split(",") == [
        "genai/gemma4:26b-a4b",
        "genai/gpt-oss:120b",
        "genai/llama4:latest",
    ]
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
    assert "silences(diff)" in workflow, "a log-level change is not a fix"
    assert "removes_error_handling(diff)" in workflow, (
        "deleting the error path is not a fix either"
    )
    assert "_unit_tests(repo, " in workflow, "no PR without the suite CI runs"
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


PROMETHEUS = REPO / "apps/monitoring/prometheus/values.yaml"
DASHBOARD = REPO / "apps/monitoring/grafana/dashboards/self-repair.json"
DASHBOARDS = REPO / "apps/monitoring/grafana/dashboards"


def test_metrics_reach_the_af_prometheus_through_its_pushgateway():
    values = yaml.safe_load(PROMETHEUS.read_text())
    gateway = values["prometheus-pushgateway"]
    assert gateway["enabled"] is True
    assert gateway["nodeSelector"] == {"cms-af-prod": "true"}, "every AF workload"
    jobs = {
        j["job_name"]: j
        for j in values["serverFiles"]["prometheus.yml"]["scrape_configs"]
    }
    job = jobs["self-repair"]
    assert job["honor_labels"] is True, "the pushed job label must survive"
    (target,) = job["static_configs"][0]["targets"]
    workflow = (WORKFLOW / "self_repair.py").read_text()
    host, port = target.split(":")
    assert f"http://{host}.cms.svc.cluster.local:{port}" in workflow, (
        "the workflow must push where Prometheus scrapes"
    )
    assert 'METRICS_JOB = "self-repair"' in workflow
    assert values["scrapeConfigs"]["prometheus-pushgateway"]["enabled"] is False, (
        "the chart's discovery-based job needs cluster RBAC this server lacks"
    )


def test_private_grafana_shows_the_workflow():
    dashboard = json.loads(DASHBOARD.read_text())
    assert dashboard["uid"] == "purdue-af-self-repair"
    uids = [json.loads(p.read_text())["uid"] for p in DASHBOARDS.glob("*.json")]
    assert uids.count(dashboard["uid"]) == 1
    for overlay in ("core-production", "core-geddes2"):
        kustomization = yaml.safe_load(
            (REPO / "deploy" / overlay / "kustomization.yaml").read_text()
        )
        (private,) = [
            g
            for g in kustomization["configMapGenerator"]
            if g["name"] == "grafana-private-dashboards"
        ]
        assert (
            "../../apps/monitoring/grafana/dashboards/self-repair.json"
            in private["files"]
        )
        assert not any(
            "self-repair.json" in f
            for g in kustomization["configMapGenerator"]
            if g["name"] != "grafana-private-dashboards"
            for f in g.get("files", [])
        ), "private dashboard only"

    triage = load_script(WORKFLOW / "triage.py", "self_repair_triage_for_dashboard")
    panels = [p for p in dashboard["panels"] if p["type"] != "row"]
    assert panels
    for panel in panels:
        assert panel["datasource"] == {"type": "prometheus", "uid": "prometheus"}
        for target in panel["targets"]:
            used = set(re.findall(r"self_repair_[a-z_]+", target["expr"]))
            assert used and used <= set(triage.METRICS), (panel["title"], used)
        stacking = (
            panel["fieldConfig"]["defaults"].get("custom", {}).get("stacking", {})
        )
        assert stacking.get("mode", "none") == "none", panel["title"]
