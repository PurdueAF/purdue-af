"""Tests for apps/interlink/* — the virtual nodes that offload AF pods to
Slurm. Nothing here talks to a cluster or to Purdue's Slurm; these assert the
wiring that otherwise fails silently *after* deployment:

  * a postRenderer patch whose target matches no rendered resource is a no-op,
    so the munge key simply never gets mounted and the node comes up unable to
    authenticate to Slurm;
  * every chart resource is named after `nodeName`, so renaming a node without
    renaming the patch target is exactly that no-op;
  * Flux's postBuild substitution rewrites `${VAR}` in these values — an
    unintended placeholder is replaced with an empty string, not left alone.

`helm template` of the same values runs in CI (validate-manifests.sh); this
suite covers what the chart itself cannot see.
"""

import re

import pytest
import yaml
from common import REPO

INTERLINK = REPO / "apps" / "interlink"


def patches(release):
    return release["spec"]["postRenderers"][0]["kustomize"]["patches"]


def ops(patch):
    return yaml.safe_load(patch["patch"])


# --- node naming ----------------------------------------------------------


def test_node_name_matches_its_directory(interlink_clusters):
    for cluster, app in interlink_clusters.items():
        assert app["values"]["nodeName"] == f"interlink-{cluster}"


def test_node_names_are_unique(interlink_clusters):
    """Two virtual kubelets registering the same Node fight over it, and the
    cleanup job of one deletes the node of the other."""
    names = [app["values"]["nodeName"] for app in interlink_clusters.values()]
    assert len(names) == len(set(names))


def test_patch_target_follows_the_node_name(interlink_clusters):
    """The chart names every resource `<nodeName>-*`. A stale target here
    matches nothing and kustomize applies the patch to no object."""
    for cluster, app in interlink_clusters.items():
        expected = f"{app['values']['nodeName']}-node"
        for patch in patches(app["release"]):
            assert patch["target"]["kind"] == "Deployment", cluster
            assert patch["target"]["name"] == expected, cluster


def test_release_name_matches_the_node(interlink_clusters):
    """Helm-release-scoped resources (`<release>-test`, `<release>-node-reader`)
    would collide between clusters in the shared namespace."""
    for cluster, app in interlink_clusters.items():
        assert app["release"]["metadata"]["name"] == f"interlink-{cluster}"


# --- the postRenderer patches --------------------------------------------


def test_patched_mounts_have_a_matching_volume(interlink_clusters):
    """A volumeMount without its volume makes the node Deployment unschedulable
    — the API server rejects the pod spec outright."""
    for cluster, app in interlink_clusters.items():
        for patch in patches(app["release"]):
            added = ops(patch)
            volumes = {
                op["value"]["name"]
                for op in added
                if op["path"] == "/spec/template/spec/volumes/-"
            }
            mounts = {
                op["value"]["name"]
                for op in added
                if op["path"].endswith("/volumeMounts/-")
            }
            assert mounts <= volumes, f"{cluster}: {mounts - volumes} unmounted"


def test_every_patch_op_is_an_add(interlink_clusters):
    """`replace`/`remove` against a path the chart may not render fails the
    whole HelmRelease; `add` on a `/-` list tail always applies."""
    for cluster, app in interlink_clusters.items():
        for patch in patches(app["release"]):
            for op in ops(patch):
                assert op["op"] == "add", cluster
                assert op["path"].endswith("/-") or op["path"].endswith("/resources")


def test_munge_key_comes_from_a_per_cluster_pvc(interlink_clusters):
    """Platform convention: munge keys live in RWX PVCs populated out of band,
    not in Secrets. The claim is per cluster — mounting another cluster's key
    makes every sbatch fail authentication."""
    for cluster, app in interlink_clusters.items():
        mounted = [
            op["value"]
            for patch in patches(app["release"])
            for op in ops(patch)
            if op["path"] == "/spec/template/spec/volumes/-"
            and op["value"]["name"] == "munge-key"
        ]
        assert len(mounted) == 1, f"{cluster}: expected exactly one munge volume"
        volume = mounted[0]
        assert "secret" not in volume, f"{cluster}: munge key must not be a Secret"
        claim = volume["persistentVolumeClaim"]["claimName"]
        assert claim == f"munge-key-{cluster}", f"{cluster}: mounts {claim}"


def test_slurm_cluster_env_matches_directory(interlink_clusters):
    """The sidecar installs /etc/slurm from /opt/purdue-af/slurm-configs/$SLURM_CLUSTER.
    A typo here points the node at the wrong RCAC controller (or refuses to start)."""
    for cluster, app in interlink_clusters.items():
        if cluster == "negishi":
            # Negishi still uses a different sidecar image without SLURM_CLUSTER.
            continue
        envs = {e["name"]: e["value"] for e in app["values"]["plugin"]["envs"]}
        assert "SLURM_CLUSTER" in envs, f"{cluster}: SLURM_CLUSTER missing"
        assert envs["SLURM_CLUSTER"] == cluster, f"{cluster}: {envs['SLURM_CLUSTER']}"


def test_active_clusters_share_one_plugin_image(active_clusters):
    """Cluster identity is SLURM_CLUSTER + munge PVC, not the image tag.
    Divergent tags mean hammer/gautschi drift on sidecar version."""
    images = {app["values"]["plugin"]["image"] for app in active_clusters.values()}
    assert len(images) == 1, f"active clusters pin different plugin images: {images}"
    image = next(iter(images))
    assert "ghcr-proxy-cache/purdueaf/interlink-slurm-plugin:" in image, image


def test_plugin_image_tag_matches_plugin_ref(active_clusters):
    """Tag == PLUGIN_REF == upstream interlink-slurm-plugin checkout. Not :latest."""
    plugin_ref = (REPO / "docker/interlink-slurm-plugin/PLUGIN_REF").read_text().strip()
    assert plugin_ref, "PLUGIN_REF is empty"
    for cluster, app in active_clusters.items():
        image = app["values"]["plugin"]["image"]
        assert image.endswith(f":{plugin_ref}"), f"{cluster}: {image} != :{plugin_ref}"
        assert not image.endswith(":latest"), cluster


def test_dockerfile_plugin_ref_arg_matches_plugin_ref_file():
    """Dockerfile ARG default must not drift from PLUGIN_REF."""
    plugin_ref = (REPO / "docker/interlink-slurm-plugin/PLUGIN_REF").read_text().strip()
    dockerfile = (REPO / "docker/interlink-slurm-plugin/Dockerfile").read_text()
    assert f"ARG SLURM_PLUGIN_REF={plugin_ref}" in dockerfile, plugin_ref


def test_plugin_image_is_not_the_old_kaniko_registry(active_clusters):
    """Kaniko pushed to geddes cms/; CI publishes to ghcr and the cluster
    pulls through ghcr-proxy-cache — the cms/ path must not come back."""
    for cluster, app in active_clusters.items():
        image = app["values"]["plugin"]["image"]
        assert "/cms/interlink-slurm-plugin:" not in image, cluster


def test_slurm_cluster_has_client_configs(interlink_clusters):
    """Baked configs live at slurm/slurm-configs-<cluster>/. Without slurm.conf
    the container exits unless /etc/secrets/slurm-configs is mounted as override."""
    slurm_root = REPO / "slurm"
    for cluster, app in interlink_clusters.items():
        if cluster == "negishi":
            # Negishi still uses a different sidecar image without SLURM_CLUSTER.
            continue
        envs = {e["name"]: e["value"] for e in app["values"]["plugin"]["envs"]}
        if "SLURM_CLUSTER" not in envs:
            continue
        conf = slurm_root / f"slurm-configs-{cluster}" / "slurm.conf"
        assert conf.is_file(), f"{cluster}: missing {conf}"
        text = conf.read_text()
        assert f"ClusterName={cluster}" in text, (
            f"{cluster}: ClusterName mismatch in {conf}"
        )
        assert "AuthType=auth/munge" in text, f"{cluster}: expected auth/munge"
        assert "SlurmctldHost=" in text, f"{cluster}: missing SlurmctldHost"


def test_munge_key_pvcs_are_not_declared_in_git(interlink_clusters, experimental):
    """Creating one from git would collide with the existing bound PVCs, whose
    specs are immutable."""
    for path in INTERLINK.rglob("*.yaml"):
        for doc in yaml.safe_load_all(path.read_text()):
            if isinstance(doc, dict) and doc.get("kind") == "PersistentVolumeClaim":
                pytest.fail(f"{path}: munge PVCs are created out of band")


def test_munge_key_material_is_never_committed():
    """The key is mounted from a PVC or a Secret created out of band. A
    `data:`/`stringData:` block here would mean it leaked into git."""
    for path in INTERLINK.rglob("*.yaml"):
        for doc in yaml.safe_load_all(path.read_text()):
            if isinstance(doc, dict) and doc.get("kind") == "Secret":
                pytest.fail(f"{path}: Secret committed in plain text")


# --- values that Flux rewrites -------------------------------------------


def test_only_namespace_is_substituted(interlink_clusters):
    """Flux postBuild substitutes every `${VAR}` in the rendered stream, and an
    unset one becomes an empty string. Bare `$VAR` (the wstunnel and tsocks
    paths) is passed through untouched, so it needs no escaping."""
    for cluster, app in interlink_clusters.items():
        text = (app["dir"] / "values.yaml").read_text()
        found = set(re.findall(r"\$\{([^}]*)\}", text))
        assert found <= {"namespace"}, f"{cluster}: {found - {'namespace'}}"


def test_plugin_envs_are_well_formed(interlink_clusters):
    """`values:` instead of `value:` is a real typo from the source config: the
    variable then reaches the container empty."""
    for cluster, app in interlink_clusters.items():
        for env in app["values"]["plugin"]["envs"]:
            assert set(env) == {"name", "value"}, f"{cluster}: {env}"
            assert isinstance(env["value"], str), f"{cluster}: {env}"


def test_extra_volume_mounts_have_a_volume(interlink_clusters):
    for cluster, app in interlink_clusters.items():
        volumes = {v["name"] for v in app["values"].get("extraVolumes", [])}
        mounts = {
            m["name"] for m in app["values"]["plugin"].get("extraVolumeMounts", [])
        }
        assert mounts <= volumes, f"{cluster}: {mounts - volumes} unmounted"


def test_chart_version_is_a_pinned_release(interlink_clusters):
    for cluster, app in interlink_clusters.items():
        spec = app["release"]["spec"]["chart"]["spec"]
        version = spec["version"]
        assert re.fullmatch(r"\d+\.\d+\.\d+", version), f"{cluster}: {version}"
        assert spec["sourceRef"]["name"] == "interlink"


# --- deployment wiring ----------------------------------------------------


def test_every_cluster_is_wired_or_explicitly_disabled(
    interlink_clusters, experimental, experimental_text
):
    """A node whose munge PVC does not exist yet is commented out rather than
    deleted — but it must be *present* either way, so a new cluster directory
    can never be silently forgotten."""
    resources = set(experimental["resources"])
    for cluster in interlink_clusters:
        entry = f"../../apps/interlink/{cluster}/helmrelease.yaml"
        assert entry in resources or f"# - {entry}" in experimental_text, cluster
    assert "../../apps/interlink/helmrepo.yaml" in resources


def test_values_configmap_is_generated_from_the_values_file(
    active_clusters, experimental
):
    """`valuesFrom` naming a ConfigMap nobody generates leaves the release
    stuck retrying forever."""
    generated = {
        g["name"]: g.get("files", []) for g in experimental["configMapGenerator"]
    }
    for cluster, app in active_clusters.items():
        name = app["release"]["spec"]["valuesFrom"][0]["name"]
        assert name in generated, f"{cluster}: {name} is not generated"
        assert (
            f"values.yaml=../../apps/interlink/{cluster}/values.yaml" in generated[name]
        )


def test_name_suffix_hash_stays_disabled(experimental):
    """valuesFrom references a fixed ConfigMap name; a hashed suffix would
    never match."""
    assert experimental["generatorOptions"]["disableNameSuffixHash"] is True


def test_values_configmap_is_not_exempt_from_substitution(experimental):
    """The plugin config needs `${namespace}` filled in, so these ConfigMaps
    must NOT carry the substitute:disabled annotation the script ConfigMaps
    use."""
    for generator in experimental["configMapGenerator"]:
        if not generator["name"].startswith("interlink-"):
            continue
        annotations = generator.get("options", {}).get("annotations", {})
        assert "kustomize.toolkit.fluxcd.io/substitute" not in annotations


# --- the hand-applied test pods ------------------------------------------


def test_test_pod_targets_its_own_virtual_node(interlink_clusters):
    for cluster, app in interlink_clusters.items():
        pod = yaml.safe_load((app["dir"] / "test-pod.yaml").read_text())
        spec = pod["spec"]
        assert (
            spec["nodeSelector"]["kubernetes.io/hostname"] == app["values"]["nodeName"]
        )
        assert any(
            t["key"] == "virtual-node.interlink/no-schedule"
            for t in spec["tolerations"]
        ), f"{cluster}: without the toleration the pod never lands on the node"


def test_disabled_clusters_generate_no_configmap(
    interlink_clusters, active_clusters, experimental
):
    """Leaving the generator behind would ship a ConfigMap for a release that
    is not deployed."""
    generated = {g["name"] for g in experimental["configMapGenerator"]}
    for cluster in set(interlink_clusters) - set(active_clusters):
        assert f"interlink-{cluster}-config" not in generated, cluster


def test_test_pods_are_not_deployed_by_flux(interlink_clusters, experimental):
    """They are one-shot debugging pods, applied by hand."""
    resources = " ".join(experimental["resources"])
    assert "test-pod.yaml" not in resources
