"""Tests for apps/ray-train/nrp_jobs.py: the RayJob a submission becomes on NRP."""

import json
import sys

import yaml
from common import REPO, load_script

sys.path.insert(0, str(REPO / "apps" / "ray-train"))
gpus = load_script(REPO / "apps" / "ray-train" / "nrp_gpus.py", "nrp_gpus")
nrp = load_script(REPO / "apps" / "ray-train" / "nrp_jobs.py", "nrp_jobs")

TEMPLATE = yaml.safe_load((REPO / "apps" / "ray-train" / "rayjob-nrp.yaml").read_text())
NODES = [
    gpus.Node("node-2", "us-west", "NVIDIA A10", 1),
    gpus.Node("node-1", "us-west", "NVIDIA A10", 2),
]


def build(data=None, **submitted):
    submission = {
        "entrypoint": "python train.py",
        "submission_id": "raysubmit_1",
    } | submitted
    return nrp.build_job(
        TEMPLATE, "ray-job-0123", "owner-key", submission, "bucket", 3, 900, data or {}
    )


def test_uploaded_packages_move_to_the_jobs_prefix_and_nothing_else_does():
    env = {
        "working_dir": "gcs://_ray_pkg_a.zip",
        "py_modules": ["gcs://_ray_pkg_b.zip", "s3://elsewhere/m.zip"],
        "pip": ["numpy"],
    }
    assert nrp.to_s3(env, "bucket", "ray-job-0123") == {
        "working_dir": "s3://bucket/ray-nrp/jobs/ray-job-0123/_ray_pkg_a.zip",
        "py_modules": [
            "s3://bucket/ray-nrp/jobs/ray-job-0123/_ray_pkg_b.zip",
            "s3://elsewhere/m.zip",
        ],
        "pip": ["numpy"],
    }


def test_a_job_runs_its_submission_suspended_until_placed():
    job = build(
        {"AF_INPUTS": "s3://bucket/ray-nrp/jobs/ray-job-0123/inputs"},
        runtime_env={"working_dir": "gcs://_ray_pkg_a.zip", "env_vars": {"A": "1"}},
        entrypoint_num_gpus=1,
        entrypoint_resources={"custom": 1},
    )
    spec = job["spec"]
    assert job["metadata"]["name"] == "ray-job-0123"
    assert job["metadata"]["labels"]["purdue-af/ray-owner"] == "owner-key"
    assert (spec["submissionMode"], spec["jobId"], spec["entrypoint"]) == (
        "K8sJobMode",
        "raysubmit_1",
        "python train.py",
    )
    assert spec["suspend"] is True
    assert (spec["shutdownAfterJobFinishes"], spec["ttlSecondsAfterFinished"]) == (
        True,
        900,
    )
    assert yaml.safe_load(spec["runtimeEnvYAML"]) == {
        "working_dir": "s3://bucket/ray-nrp/jobs/ray-job-0123/_ray_pkg_a.zip",
        "env_vars": {
            "A": "1",
            "AF_INPUTS": "s3://bucket/ray-nrp/jobs/ray-job-0123/inputs",
        },
    }
    assert spec["entrypointNumGpus"] == 1
    assert json.loads(spec["entrypointResources"]) == {"custom": 1}
    assert nrp.workers_of(job) == 3
    assert nrp.held_gpus(job) == 3


def test_a_placed_job_runs_its_workers_on_its_nodes_and_its_head_in_their_region():
    job = build()
    cluster = nrp.placed(job, 2, NODES)
    (group,) = cluster["workerGroupSpecs"]
    assert (group["replicas"], group["minReplicas"], group["maxReplicas"]) == (2, 2, 2)
    (term,) = group["template"]["spec"]["affinity"]["nodeAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ]["nodeSelectorTerms"]
    assert term["matchExpressions"] == [
        {
            "key": "kubernetes.io/hostname",
            "operator": "In",
            "values": ["node-1", "node-2"],
        }
    ]
    head = cluster["headGroupSpec"]["template"]["spec"]
    assert head["nodeSelector"] == {"topology.kubernetes.io/region": "us-west"}
    for pod in (head, group["template"]["spec"]):
        (container,) = pod["containers"]
        sized = [e for e in container["env"] if e["name"] == "AF_MAX_WORKERS"]
        assert sized == [{"name": "AF_MAX_WORKERS", "value": "2"}]
    # The job itself keeps its cluster until the patch reaches NRP.
    assert "affinity" not in json.dumps(job)


def test_a_jobs_data_is_under_its_prefix():
    assert (
        nrp.data_uri("bucket", "ray-job-0123", "output")
        == "s3://bucket/ray-nrp/jobs/ray-job-0123/output"
    )


def test_a_job_without_a_runtime_environment_names_none():
    assert "runtimeEnvYAML" not in build()["spec"]


def test_building_a_job_leaves_the_template_alone():
    before = json.dumps(TEMPLATE, sort_keys=True)
    build()
    assert json.dumps(TEMPLATE, sort_keys=True) == before


def test_an_owner_key_names_no_one_and_tells_a_person_from_an_account():
    person = nrp.owner_key("ray-train-demo-workload", "")
    account = nrp.owner_key("ray-train-demo-workload", "demo-workload")
    assert person != account
    assert "demo" not in person + account
    assert len(person) <= 63
