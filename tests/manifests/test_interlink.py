"""Tests for apps/interlink/* — the virtual nodes that offload AF pods to a
remote system, Slurm or another Kubernetes cluster. Nothing here talks to a
cluster or to Purdue's Slurm; these assert the wiring that otherwise fails
silently *after* deployment:

  * a node without its remote credential comes up unable to authenticate;
  * Flux's postBuild substitution rewrites `${VAR}` in these values — an
    unintended placeholder is replaced with an empty string, not left alone.

`helm template` of the same values runs in CI (validate-manifests.sh); this
suite covers what the chart itself cannot see.
"""

import configparser
import re
from pathlib import Path

import pytest
import yaml
from common import REPO

INTERLINK = REPO / "apps" / "interlink"

# Where the kubernetes plugin image resolves its relative paths against
# (WORKDIR in upstream's dockerfile-prod).
PLUGIN_WORKDIR = Path("/interlink-kubernetes-plugin")


def _backend(app):
    """The plugin image names the sidecar's kind."""
    image = app["values"]["plugin"]["image"]
    known = ("interlink-slurm-plugin", "interlink-kubernetes-plugin")
    assert any(p in image for p in known), image
    return "slurm" if "interlink-slurm-plugin" in image else "kubernetes"


@pytest.fixture(scope="session")
def slurm_clusters(interlink_clusters):
    return {c: app for c, app in interlink_clusters.items() if _backend(app) == "slurm"}


@pytest.fixture(scope="session")
def kubernetes_clusters(interlink_clusters):
    return {
        c: app for c, app in interlink_clusters.items() if _backend(app) == "kubernetes"
    }


@pytest.fixture(scope="session")
def active_slurm_clusters(active_clusters):
    return {c: app for c, app in active_clusters.items() if _backend(app) == "slurm"}


# --- node naming ----------------------------------------------------------


def test_node_name_matches_its_directory(interlink_clusters):
    for cluster, app in interlink_clusters.items():
        assert app["values"]["nodeName"] == f"interlink-{cluster}"


def test_node_names_are_unique(interlink_clusters):
    """Two virtual kubelets registering the same Node fight over it, and the
    cleanup job of one deletes the node of the other."""
    names = [app["values"]["nodeName"] for app in interlink_clusters.values()]
    assert len(names) == len(set(names))


def test_release_name_matches_the_node(interlink_clusters):
    """Helm-release-scoped resources (`<release>-test`, `<release>-node-reader`)
    would collide between clusters in the shared namespace."""
    for cluster, app in interlink_clusters.items():
        assert app["release"]["metadata"]["name"] == f"interlink-{cluster}"


# --- the munge key (Slurm-backed nodes) -------------------------------------


def test_munge_key_comes_from_a_per_cluster_pvc(slurm_clusters):
    """Platform convention: munge keys live in RWX PVCs populated out of band,
    not in Secrets. The claim is per cluster — mounting another cluster's key
    makes every sbatch fail authentication."""
    for cluster, app in slurm_clusters.items():
        mounted = [
            volume
            for volume in app["values"].get("extraVolumes", [])
            if volume["name"] == "munge-key"
        ]
        assert len(mounted) == 1, f"{cluster}: expected exactly one munge volume"
        volume = mounted[0]
        assert "secret" not in volume, f"{cluster}: munge key must not be a Secret"
        claim = volume["persistentVolumeClaim"]["claimName"]
        assert claim == f"munge-key-{cluster}", f"{cluster}: mounts {claim}"


def test_plugin_reads_the_munge_key_where_its_startup_looks(slurm_clusters):
    """The Slurm sidecar is the container that authenticates; the key is not
    its to change."""
    for cluster, app in slurm_clusters.items():
        mounts = {
            mount["name"]: mount
            for mount in app["values"]["plugin"].get("extraVolumeMounts", [])
        }
        assert "munge-key" in mounts, f"{cluster}: munge key not mounted"
        assert mounts["munge-key"]["mountPath"] == "/etc/secrets/munge", cluster
        assert mounts["munge-key"]["readOnly"] is True, cluster


def test_slurm_cluster_env_matches_directory(active_slurm_clusters):
    """The sidecar installs /etc/slurm from /opt/purdue-af/slurm-configs/$SLURM_CLUSTER.
    A typo here points the node at the wrong RCAC controller (or refuses to start).
    Clusters commented out in experimental are not part of this contract.
    """
    for cluster, app in active_slurm_clusters.items():
        envs = {e["name"]: e["value"] for e in app["values"]["plugin"]["envs"]}
        assert "SLURM_CLUSTER" in envs, f"{cluster}: SLURM_CLUSTER missing"
        assert envs["SLURM_CLUSTER"] == cluster, f"{cluster}: {envs['SLURM_CLUSTER']}"


def test_slurm_clusters_share_one_plugin_image(active_slurm_clusters):
    """Cluster identity is SLURM_CLUSTER + munge PVC, not the image tag.
    Divergent tags mean clusters drift on sidecar version."""
    images = {
        app["values"]["plugin"]["image"] for app in active_slurm_clusters.values()
    }
    assert len(images) == 1, f"active clusters pin different plugin images: {images}"
    image = next(iter(images))
    assert "ghcr-proxy-cache/purdueaf/interlink-slurm-plugin:" in image, image


def test_plugin_image_tag_matches_plugin_ref(active_slurm_clusters):
    """Tag == PLUGIN_REF == upstream interlink-slurm-plugin checkout. Not :latest."""
    plugin_ref = (REPO / "docker/interlink-slurm-plugin/PLUGIN_REF").read_text().strip()
    assert plugin_ref, "PLUGIN_REF is empty"
    for cluster, app in active_slurm_clusters.items():
        image = app["values"]["plugin"]["image"]
        assert image.endswith(f":{plugin_ref}"), f"{cluster}: {image} != :{plugin_ref}"
        assert not image.endswith(":latest"), cluster


def test_dockerfile_plugin_ref_arg_matches_plugin_ref_file():
    """Dockerfile ARG default must not drift from PLUGIN_REF."""
    plugin_ref = (REPO / "docker/interlink-slurm-plugin/PLUGIN_REF").read_text().strip()
    dockerfile = (REPO / "docker/interlink-slurm-plugin/Dockerfile").read_text()
    assert f"ARG SLURM_PLUGIN_REF={plugin_ref}" in dockerfile, plugin_ref


def test_slurm_cluster_has_client_configs(active_slurm_clusters):
    """Baked configs live at slurm/slurm-configs-<cluster>/. Without slurm.conf
    the container exits unless /etc/secrets/slurm-configs is mounted as override.
    """
    slurm_root = REPO / "slurm"
    for cluster in active_slurm_clusters:
        conf = slurm_root / f"slurm-configs-{cluster}" / "slurm.conf"
        assert conf.is_file(), f"{cluster}: missing {conf}"
        text = conf.read_text()
        assert f"ClusterName={cluster}" in text, (
            f"{cluster}: ClusterName mismatch in {conf}"
        )
        assert "AuthType=auth/munge" in text, f"{cluster}: expected auth/munge"
        assert "SlurmctldHost=" in text, f"{cluster}: missing SlurmctldHost"


def _client_versions():
    """Parse slurm/client-versions -> {cluster: version}."""
    path = REPO / "slurm" / "client-versions"
    mapping = {}
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        cluster, version, *rest = line.split()
        assert not rest, f"extra fields in client-versions: {line!r}"
        mapping[cluster] = version
    return mapping


def test_every_slurm_cluster_has_a_slurm_client_version(slurm_clusters):
    """startup.sh refuses to start without a version; a new cluster directory
    must not be forgotten in client-versions."""
    versions = _client_versions()
    for cluster in slurm_clusters:
        assert cluster in versions, f"{cluster}: missing from slurm/client-versions"


def test_slurm_client_rpms_exist_for_mapped_versions(slurm_clusters):
    """The Dockerfile extracts slurm/slurm-<ver>-1.el8.x86_64.rpm per version.
    A map entry without the RPM fails the image build — catch it in unit tests."""
    versions = _client_versions()
    for cluster in slurm_clusters:
        ver = versions[cluster]
        rpm = REPO / "slurm" / f"slurm-{ver}-1.el8.x86_64.rpm"
        assert rpm.is_file(), f"{cluster}: missing {rpm.name} for version {ver}"


# --- the remote kubeconfig (Kubernetes-backed nodes) -----------------------


def _plugin_ini(app):
    ini = configparser.ConfigParser()
    ini.read_string(app["values"]["plugin"]["config"])
    return ini


def test_kubeconfig_comes_from_a_secret_created_out_of_band(kubernetes_clusters):
    """The kubeconfig is a Secret in cms created out of band (never in git),
    mounted read-only and private."""
    for cluster, app in kubernetes_clusters.items():
        mounted = [
            volume
            for volume in app["values"].get("extraVolumes", [])
            if volume["name"] == "kubeconfig"
        ]
        assert len(mounted) == 1, f"{cluster}: expected exactly one kubeconfig volume"
        volume = mounted[0]
        assert "persistentVolumeClaim" not in volume, f"{cluster}: {volume}"
        assert volume["secret"]["secretName"] == f"kubeconfig-{cluster}-interlink", (
            f"{cluster}: {volume}"
        )
        assert volume["secret"]["defaultMode"] == 0o400, f"{cluster}: {volume}"


def test_plugin_opens_the_kubeconfig_where_it_is_mounted(kubernetes_clusters):
    """The plugin resolves its kubeconfig_path against the image's WORKDIR;
    a mountPath that moves without the INI following leaves the plugin
    hunting a file that is not there. The secret's key names the file."""
    for cluster, app in kubernetes_clusters.items():
        mounts = {
            mount["name"]: mount
            for mount in app["values"]["plugin"]["extraVolumeMounts"]
        }
        assert "kubeconfig" in mounts, f"{cluster}: kubeconfig not mounted"
        assert mounts["kubeconfig"]["readOnly"] is True, cluster
        path = PLUGIN_WORKDIR / _plugin_ini(app).get("k8s", "kubeconfig_path")
        assert path.name == "kubeconfig.yaml", f"{cluster}: {path}"
        assert path.parent == Path(mounts["kubeconfig"]["mountPath"]), (
            f"{cluster}: {path} is not the mounted file"
        )


def test_plugin_config_carries_the_metadata_fastapi_requires(kubernetes_clusters):
    """The plugin passes [app] name/description/version straight into its
    FastAPI constructor, which asserts a non-empty version — a key trimmed
    from the INI crashes the container at import time."""
    for cluster, app in kubernetes_clusters.items():
        ini = _plugin_ini(app)
        for key in ("name", "description", "version"):
            assert ini.get("app", key, fallback=""), f"{cluster}: [app] {key} is empty"


def test_plugin_listens_where_the_api_server_calls_it(kubernetes_clusters):
    """The chart aims the interLink API at plugin.address:plugin.port; the
    plugin listens where its own INI says. A mismatch fails every offload
    with a connection refused."""
    for cluster, app in kubernetes_clusters.items():
        ini = _plugin_ini(app)
        assert ini.get("app", "socket_address") == app["values"]["plugin"]["address"], (
            cluster
        )
        assert ini.get("app", "socket_port") == str(app["values"]["plugin"]["port"]), (
            cluster
        )


def test_plugin_reads_the_config_the_chart_mounts(kubernetes_clusters):
    """The chart mounts plugin.config at /etc/interlink/plugin.yaml. Without
    CONFIG_FILE_PATH pointing there, the plugin reads the sample config baked
    into its image and connects to nothing."""
    for cluster, app in kubernetes_clusters.items():
        envs = {e["name"]: e["value"] for e in app["values"]["plugin"]["envs"]}
        assert envs.get("CONFIG_FILE_PATH") == "/etc/interlink/plugin.yaml", cluster


def test_kubernetes_plugin_image_is_pinned(kubernetes_clusters):
    """Through the cluster registry's Docker Hub cache, so Renovate tracks
    the upstream tag. Not :latest."""
    for cluster, app in kubernetes_clusters.items():
        image = app["values"]["plugin"]["image"]
        assert "docker-hub-cache/mginfn/interlink-kubernetes-plugin:" in image, image
        assert not image.endswith(":latest"), cluster


def test_offloaded_namespace_matches_the_remote_rbac(kubernetes_clusters):
    """The plugin prepends namespace_prefix to the pod's namespace (cms);
    the remote RBAC, applied out of band, must cover exactly that namespace
    or every offloaded pod create is a 403."""
    for cluster, app in kubernetes_clusters.items():
        prefix = _plugin_ini(app).get("offloading", "namespace_prefix")
        rbac_files = sorted((app["dir"] / "to-deploy-at-nrp").glob("rbac-*.yaml"))
        assert len(rbac_files) == 1, f"{cluster}: {rbac_files}"
        namespaces = {
            doc["metadata"]["namespace"]
            for doc in yaml.safe_load_all(rbac_files[0].read_text())
            if isinstance(doc, dict) and doc.get("metadata", {}).get("namespace")
        }
        assert f"{prefix}-cms" in namespaces, (
            f"{cluster}: {prefix}-cms not in {namespaces}"
        )


def test_remote_rbac_lets_the_plugin_find_the_offloading_namespace(kubernetes_clusters):
    """The plugin lists namespaces at cluster scope before every pod create;
    without a namespace-reader ClusterRole every offload dies on a 403 the
    moment the pod is submitted."""
    for cluster, app in kubernetes_clusters.items():
        rbac_files = sorted((app["dir"] / "to-deploy-at-nrp").glob("rbac-*.yaml"))
        docs = [
            doc
            for doc in yaml.safe_load_all(rbac_files[0].read_text())
            if isinstance(doc, dict)
        ]
        grants_read = any(
            doc.get("kind") == "ClusterRole"
            and any(
                rule.get("resources") == ["namespaces"]
                and {"get", "list"} <= set(rule.get("verbs", []))
                for rule in doc.get("rules", [])
            )
            for doc in docs
        )
        assert grants_read, f"{cluster}: no ClusterRole granting namespace get/list"


def test_kubernetes_nodes_are_batch_only(kubernetes_clusters):
    """The Kubernetes plugin runs no wstunnel client, so EnableTunnel would
    stand up shadow infrastructure nothing ever connects to; full mesh is the
    feature that makes offloaded pods reachable, and it is a deliberate
    non-goal until a service needs it."""
    for cluster, app in kubernetes_clusters.items():
        network = app["values"]["virtualNode"].get("network", {})
        assert not network.get("enableTunnel"), (
            f"{cluster}: enableTunnel is dead weight"
        )
        assert not network.get("fullMesh"), f"{cluster}: full mesh is not configured"


# --- credentials stay out of git --------------------------------------------


def test_pvcs_are_not_declared_in_git(interlink_clusters):
    """Out-of-band volumes (the munge PVCs) are created by hand; a git-declared
    PVC would collide with the existing bound one, whose spec is immutable."""
    for path in INTERLINK.rglob("*.yaml"):
        for doc in yaml.safe_load_all(path.read_text()):
            if isinstance(doc, dict) and doc.get("kind") == "PersistentVolumeClaim":
                pytest.fail(f"{path}: out-of-band PVC declared in git")


def test_credentials_are_never_committed():
    """The munge key is mounted from a PVC, the kubeconfig from a Secret
    created out of band. A `data:`/`stringData:` block here would mean a
    credential leaked into git."""
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
    """A node whose credential does not exist yet is commented out rather than
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
    """The Slurm plugin config needs `${namespace}` filled in, so these
    ConfigMaps must NOT carry the substitute:disabled annotation the script
    ConfigMaps use."""
    for generator in experimental["configMapGenerator"]:
        if not generator["name"].startswith("interlink-"):
            continue
        annotations = generator.get("options", {}).get("annotations", {})
        assert "kustomize.toolkit.fluxcd.io/substitute" not in annotations


# --- Slurm client activation -----------------------------------------------
#
# Relocating the client RPMs under /opt/purdue-af/slurm-clients/<ver>/ so two
# versions can coexist broke every one of them: the binaries keep the absolute
# paths they were compiled with. sbatch has RPATH=/usr/lib64/slurm and Slurm
# reads PluginDir from that same compiled-in path, so with the directory gone
# sbatch exits 127 on libslurmfull.so and no job is ever submitted. Nothing in
# the manifests can see this — it takes actually running the image.

STARTUP = REPO / "docker/interlink-slurm-plugin" / "startup.sh"
DOCKERFILE = REPO / "docker/interlink-slurm-plugin" / "Dockerfile"


def test_activation_points_the_compiled_in_plugin_dir_at_the_chosen_version():
    """ld.so.conf covers libslurm.so, but reaches neither the RPATH nor the
    PluginDir lookup; only /usr/lib64/slurm itself satisfies those."""
    startup = STARTUP.read_text()
    link = 'ln -sfn "${client_root}/usr/lib64/slurm" "${sys_plugin_dir}"'
    assert link in startup, "startup.sh must link the versioned plugin dir"
    assert 'sys_plugin_dir="/usr/lib64/slurm"' in startup


def test_activation_refuses_to_shadow_a_real_system_plugin_dir():
    """`ln -sfn` onto an existing directory silently creates the link *inside*
    it, which would leave one client version loading another's plugins."""
    startup = STARTUP.read_text()
    assert '[ -e "${sys_plugin_dir}" ] && [ ! -L "${sys_plugin_dir}" ]' in startup


def test_image_build_proves_each_extracted_client_can_run():
    """`sbatch --version` needs no slurm.conf and no controller, yet exercises
    both the dynamic linker and the PluginDir check — the two things that
    relocation breaks. Without it, breakage only shows up in a running pod."""
    dockerfile = DOCKERFILE.read_text()
    assert '"${dest}/usr/bin/sbatch" --version' in dockerfile, (
        "no build-time smoke test that the extracted client actually runs"
    )
    assert 'ln -sfn "${dest}/usr/lib64/slurm" /usr/lib64/slurm' in dockerfile
    assert "rm -f /usr/lib64/slurm" in dockerfile, (
        "build-time link must be removed, or startup.sh refuses to activate"
    )
    # Without SLURM_CONF, sbatch --version hunts for a controller via DNS SRV.
    assert "SLURM_CONF=/tmp/smoke.conf" in dockerfile, (
        "smoke test needs a throwaway config or it fails for the wrong reason"
    )
    assert "smoke.conf" in dockerfile.split("rm -rf /etc/slurm/*")[1], (
        "the throwaway config must not ship in the image"
    )
