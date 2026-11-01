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
import json
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


@pytest.fixture(scope="session")
def active_kubernetes_clusters(active_clusters):
    return {
        c: app for c, app in active_clusters.items() if _backend(app) == "kubernetes"
    }


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


def test_slurm_commands_run_the_client_startup_activates(slurm_clusters):
    """The image keeps one Slurm client per version and startup.sh links the
    chosen one into a single directory; nothing is installed in /usr/bin, so a
    path elsewhere fails every submission."""
    startup = (REPO / "docker/interlink-slurm-plugin/startup.sh").read_text()
    active_bin = re.search(r'^active_bin="([^"]+)"', startup, re.M).group(1)
    for cluster, app in slurm_clusters.items():
        config = yaml.safe_load(app["values"]["plugin"]["config"])
        for key, tool in (
            ("SbatchPath", "sbatch"),
            ("ScancelPath", "scancel"),
            ("SqueuePath", "squeue"),
        ):
            assert config.get(key) == f"{active_bin}/{tool}", (
                f"{cluster}: {key}={config.get(key)}"
            )


def test_plugin_image_tag_matches_plugin_ref(active_clusters):
    """Tag == PLUGIN_REF == the upstream ref each site image builds from.
    Not :latest."""
    for cluster, app in active_clusters.items():
        backend = _backend(app)
        plugin_ref = (
            (REPO / f"docker/interlink-{backend}-plugin/PLUGIN_REF").read_text().strip()
        )
        assert plugin_ref, f"{backend}: PLUGIN_REF is empty"
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


def test_token_renewal_mounts_the_kubeconfig_as_the_plugin_does(kubernetes_clusters):
    """Its exec user reads the token at the plugin's path, in the plugin's image."""
    app = kubernetes_clusters["nrp"]
    renewal = INTERLINK / "nrp" / "token-renewal"
    cronjob = yaml.safe_load((renewal / "cronjob.yaml").read_text())
    pod = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    (container,) = pod["containers"]
    assert container["image"] == app["values"]["plugin"]["image"]
    plugin_mount = {m["name"]: m for m in app["values"]["plugin"]["extraVolumeMounts"]}[
        "kubeconfig"
    ]
    mount = {m["name"]: m for m in container["volumeMounts"]}["kubeconfig"]
    assert mount["mountPath"] == plugin_mount["mountPath"]
    volumes = {v["name"]: v for v in pod["volumes"]}
    plugin_volumes = {v["name"]: v for v in app["values"]["extraVolumes"]}
    assert volumes["kubeconfig"] == plugin_volumes["kubeconfig"]
    assert f'MOUNT = Path("{mount["mountPath"]}")' in (renewal / "renew.py").read_text()


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


def test_kubernetes_plugin_image_is_the_site_build(active_kubernetes_clusters):
    """Through the cluster registry's ghcr proxy cache. The upstream image
    lists namespaces at cluster scope before every pod create — a request
    NRP's namespaced-only RBAC forbids; the site image is the one carrying
    the ensure-namespace patch."""
    for cluster, app in active_kubernetes_clusters.items():
        image = app["values"]["plugin"]["image"]
        assert "ghcr-proxy-cache/purdueaf/interlink-kubernetes-plugin:" in image, image


def test_kubernetes_dockerfile_upstream_image_arg_matches_plugin_ref_file():
    """Dockerfile ARG default must not drift from PLUGIN_REF."""
    plugin_ref = (
        (REPO / "docker/interlink-kubernetes-plugin/PLUGIN_REF").read_text().strip()
    )
    dockerfile = (REPO / "docker/interlink-kubernetes-plugin/Dockerfile").read_text()
    assert (
        f"ARG UPSTREAM_IMAGE=docker.io/mginfn/interlink-kubernetes-plugin:{plugin_ref}"
        in dockerfile
    ), plugin_ref


def test_plugin_leaves_the_offloading_namespace_to_the_rbac(active_kubernetes_clusters):
    """The site image gates the plugin's cluster-scope namespace list behind
    [offloading] ensure_namespace; without an explicit False the upstream
    default re-enables it and every offload dies on a 403."""
    for cluster, app in active_kubernetes_clusters.items():
        value = _plugin_ini(app).get("offloading", "ensure_namespace", fallback=None)
        assert value is not None, f"{cluster}: ensure_namespace not set"
        assert value.lower() == "false", f"{cluster}: ensure_namespace={value}"


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


def test_remote_rbac_lets_the_plugin_poll_pod_status(kubernetes_clusters):
    """The plugin reads the pods/status subresource on every status poll;
    `get` on pods does not cover it, and without the grant an offloaded pod
    never leaves `Remote pod submitted` on the local cluster."""
    for cluster, app in kubernetes_clusters.items():
        rbac_files = sorted((app["dir"] / "to-deploy-at-nrp").glob("rbac-*.yaml"))
        docs = [
            doc
            for doc in yaml.safe_load_all(rbac_files[0].read_text())
            if isinstance(doc, dict)
        ]
        grants_read = any(
            doc.get("kind") == "Role"
            and any(
                rule.get("resources") == ["pods/status"]
                and "get" in rule.get("verbs", [])
                for rule in doc.get("rules", [])
            )
            for doc in docs
        )
        assert grants_read, f"{cluster}: no Role granting get on pods/status"


def _tunnel_nodes(clusters):
    for cluster, app in clusters.items():
        network = app["values"]["virtualNode"].get("network", {})
        if network.get("enableTunnel"):
            yield cluster, app, network


def test_kubernetes_tunnel_has_a_client(kubernetes_clusters):
    """EnableTunnel stands up a shadow pod per offloaded pod with ports; only
    the site plugin's wstunnel-client sidecar ever connects to it, and only
    when [wstunnel] client_image is set. The client speaks the protocol of the
    shadow's image."""
    for cluster, app, network in _tunnel_nodes(kubernetes_clusters):
        client_image = _plugin_ini(app).get("wstunnel", "client_image", fallback="")
        assert client_image, f"{cluster}: enableTunnel without a client sidecar"
        assert f"image: {client_image}\n" in network.get("customTemplate", ""), cluster


def test_slurm_tunnel_client_matches_the_shadow_image(slurm_clusters):
    """The Slurm job downloads the wstunnel release its command names; the
    client speaks the protocol of the shadow's image."""
    for cluster, _, network in _tunnel_nodes(slurm_clusters):
        tags = re.findall(r"WSTUNNEL_TAG=(\S+)", network.get("wstunnelCommand", ""))
        assert len(tags) == 1, f"{cluster}: no single WSTUNNEL_TAG"
        image = f"ghcr.io/erebe/wstunnel:{tags[0]}"
        assert f"image: {image}\n" in network.get("customTemplate", ""), cluster


def test_tunnel_command_uses_every_argument(interlink_clusters):
    """The virtual kubelet fills the command with fmt.Sprintf(path prefix,
    -R options, shadow hostname); one dropped leaves the tunnel unauthorised,
    portless or misrouted. The Kubernetes sidecar runs it as its main
    process, so a backgrounded command exits the sidecar at once; a Slurm
    job runs it ahead of the pod's own command, which a foreground client
    never lets start."""
    for cluster, app, network in _tunnel_nodes(interlink_clusters):
        command = network.get("wstunnelCommand", "")
        indexed = {int(n) for n in re.findall(r"%\[(\d)\]s", command)}
        plain = len(re.findall(r"%s", command))
        assert indexed == {1, 2, 3} or (not indexed and plain == 3), (
            f"{cluster}: {command}"
        )
        backgrounded = command.rstrip().endswith("&")
        assert backgrounded == (_backend(app) == "slurm"), cluster


def test_tunnel_nodes_share_one_shadow_template(interlink_clusters):
    """Both nodes' shadows sit behind the same Geddes ingress; the template
    differs only by mistake."""
    templates = {
        network.get("customTemplate")
        for _, _, network in _tunnel_nodes(interlink_clusters)
    }
    assert len(templates) == 1, "shadow templates differ"


def _render_shadow(template, prefix, ports):
    """The virtual kubelet's text/template render, for the actions the shadow
    template uses: fields, `range .ExposedPorts` and `{{-` trimming."""
    text = re.sub(r"\s*\{\{-\s*", "{{", template)
    text = re.sub(
        r"\{\{range \.ExposedPorts\}\}(.*?)\{\{end\}\}",
        lambda m: "".join(
            re.sub(r"\{\{\.(\w+)\}\}", lambda f: str(port[f[1]]), m[1])
            for port in ports
        ),
        text,
        flags=re.S,
    )
    fields = {
        "Name": "shadow",
        "Namespace": "cms",
        "RandomPassword": prefix,
        "WildcardDNS": "example.org",
    }
    return re.sub(r"\{\{\.(\w+)\}\}", lambda f: fields[f[1]], text)


class _TaggedLoader(yaml.SafeLoader):
    """wstunnel's restriction file tags each rule with its kind."""


_TaggedLoader.add_multi_constructor(
    "!",
    lambda loader, tag, node: {
        tag: loader.construct_mapping(node, deep=True)
        if isinstance(node, yaml.MappingNode)
        else loader.construct_scalar(node)
    },
)


def test_shadow_server_allows_only_the_clients_reverse_tunnels(interlink_clusters):
    """The remote side opens `-R tcp://0.0.0.0:<port>:localhost:<port>` per
    exposed TCP port, with the pod's path prefix; the shadow's wstunnel server
    allows exactly those tunnels. The virtual kubelet splits the rendered
    template on every "---"."""
    prefix = "0123456789abcdef"
    ports = [
        {"Port": 8000, "Name": "http", "Protocol": "TCP"},
        {"Port": 8001, "Name": "grpc", "Protocol": "TCP"},
    ]
    for cluster, _, network in _tunnel_nodes(interlink_clusters):
        rendered = _render_shadow(network["customTemplate"], prefix, ports)
        docs = [yaml.safe_load(part) for part in rendered.split("---") if part.strip()]
        (deployment,) = [doc for doc in docs if doc["kind"] == "Deployment"]
        (container,) = deployment["spec"]["template"]["spec"]["containers"]
        assert container["command"] == ["/bin/sh", "-c"], cluster
        script = container["args"][0]
        restrictions = re.search(
            r"^cat >(\S+) <<'EOF'\n(.*?)^EOF$", script, re.S | re.M
        )
        assert restrictions, f"{cluster}: the server loads no restriction file"
        server = script[restrictions.end() :]
        assert f"--restrict-config {restrictions[1]} " in server, cluster
        rules = yaml.load(restrictions[2], Loader=_TaggedLoader)["restrictions"]
        matches = [rule["match"] for rule in rules]
        assert matches == [[{"PathPrefix": f"^{prefix}$"}]], cluster
        assert [allow for rule in rules for allow in rule["allow"]] == [
            {"ReverseTunnel": {"protocol": ["Tcp"], "port": [str(p["Port"])]}}
            for p in ports
        ], cluster


def _tunnel_traefik():
    return yaml.safe_load((INTERLINK / "traefik" / "values.yaml").read_text())


def test_tunnel_ingress_is_served_by_the_tunnel_traefik(interlink_clusters):
    """The shared nginx ingress closes open connections about 240 s after any
    reload; the shadow Ingress belongs to apps/interlink/traefik instead.
    Namespaced, that Traefik ignores spec.ingressClassName and matches the
    annotation, and nginx must not claim the Ingress."""
    tunnel_class = _tunnel_traefik()["providers"]["kubernetesIngress"]["ingressClass"]
    for cluster, _, network in _tunnel_nodes(interlink_clusters):
        parts = network["customTemplate"].split("\n---\n")
        ingresses = [
            yaml.safe_load(re.sub(r"\{\{[^}]*\}\}", "x", part))
            for part in parts
            if re.search(r"^kind: Ingress$", part, re.M)
        ]
        assert ingresses, f"{cluster}: no shadow Ingress"
        for ingress in ingresses:
            annotations = ingress["metadata"].get("annotations") or {}
            assert annotations.get("kubernetes.io/ingress.class") == tunnel_class, (
                cluster
            )
            assert "ingressClassName" not in ingress["spec"], cluster


def test_tunnel_shadow_outlives_the_remote_delete(interlink_clusters):
    """The virtual kubelet deletes the shadow before it asks the plugin to
    delete the remote pod; the shadow's preStop keeps the tunnel up through
    the plugin's delete delay, and the grace period lets the preStop finish."""
    for cluster, app, network in _tunnel_nodes(interlink_clusters):
        parts = network["customTemplate"].split("\n---\n")
        (deployment,) = [
            yaml.safe_load(
                re.sub(r"\{\{[^}]*\}\}", "x", re.sub(r"(?m)^\s*\{\{-.*\n", "", part))
            )
            for part in parts
            if re.search(r"^kind: Deployment$", part, re.M)
        ]
        pod = deployment["spec"]["template"]["spec"]
        (container,) = pod["containers"]
        command = container["lifecycle"]["preStop"]["exec"]["command"]
        assert command[0] == "sleep", cluster
        delay = 0.0
        if _backend(app) == "kubernetes":
            delay = _plugin_ini(app).getfloat(
                "offloading", "delete_delay_seconds", fallback=0.0
            )
        assert delay < float(command[1]) < pod["terminationGracePeriodSeconds"], cluster


def test_tunnel_client_dials_the_pinned_traefik_address(interlink_clusters):
    """No DNS name points at the Traefik; the client dials its address, which
    MetalLB holds only while the Service pins it."""
    annotations = _tunnel_traefik()["service"]["annotations"]
    address = annotations["metallb.universe.tf/loadBalancerIPs"]
    assert annotations["metallb.universe.tf/address-pool"] == "geddes-public-pool"
    for cluster, _, network in _tunnel_nodes(interlink_clusters):
        targets = re.findall(r"wss?://([^\s:/]+)", network.get("wstunnelCommand", ""))
        assert targets == [address], f"{cluster}: dials {targets}"


# A pod's name becomes "wstunnel-<pod>-<ns>", then "<that>-<ns>" as the
# shadow hostname's first label; past 63 characters the virtual kubelet
# truncates the pod name, and replicas of one Deployment collide. A
# Deployment's pods add "-<pod-template-hash, <=10>-<5>" to its name.
_POD_SUFFIX = len("-0123456789-abcde")


def test_supersonic_on_a_tunnel_node_gets_a_unique_shadow(interlink_clusters):
    """SuperSONIC releases on a tunnelled node keep the shadow in cms, the
    only namespace the facility uses, and name their inference server short
    enough that each replica's shadow is its own."""
    tunnel_nodes = {
        app["values"]["nodeName"] for _, app, _ in _tunnel_nodes(interlink_clusters)
    }
    checked = set()
    for values_file in sorted((REPO / "apps" / "sonic").glob("*/values.yaml")):
        values = yaml.safe_load(values_file.read_text())
        server = values.get("inferenceServer", {})
        node = server.get("nodeSelector", {}).get("kubernetes.io/hostname")
        if node not in tunnel_nodes:
            continue
        checked.add(node)
        release = yaml.safe_load((values_file.parent / "helmrelease.yaml").read_text())[
            "metadata"
        ]["name"]
        name = values.get("nameOverride") or release
        pod = f"{name}-inference-server" + "x" * _POD_SUFFIX
        label = f"wstunnel-{pod}-cms-cms"
        assert len(label) <= 63, f"{values_file}: shadow label {len(label)} > 63"
        annotations = server.get("annotations", {})
        assert annotations.get("interlink.eu/shadow-same-ns") == "true", values_file
    assert checked == tunnel_nodes, "a tunnelled node runs no SuperSONIC release"


def test_kubernetes_remote_pods_never_restart_in_place(kubernetes_clusters):
    """The virtual kubelet reports a remote restart as a failed local pod,
    whose controller makes a replacement while the restarted remote pod keeps
    its GPU."""
    for cluster, app in kubernetes_clusters.items():
        policy = _plugin_ini(app).get("offloading", "restart_policy", fallback="")
        assert policy == "Never", f"{cluster}: restart_policy={policy!r}"


def test_remote_affinity_annotations_are_node_affinity_json(interlink_clusters):
    """The plugin loads the annotation as a core/v1 Affinity; malformed JSON
    fails every pod create, and an unknown key is silently dropped."""
    nodes = {
        app["values"]["nodeName"]
        for app in interlink_clusters.values()
        if _backend(app) == "kubernetes"
    }
    checked = 0
    for values_file in sorted((REPO / "apps").glob("**/values.yaml")):
        values = yaml.safe_load(values_file.read_text()) or {}
        server = values.get("inferenceServer", {}) if isinstance(values, dict) else {}
        raw = (server.get("annotations") or {}).get("interlink.eu/remote-affinity")
        if raw is None:
            continue
        node = (server.get("nodeSelector") or {}).get("kubernetes.io/hostname")
        assert node in nodes, f"{values_file}: remote affinity off a Kubernetes node"
        affinity = json.loads(raw)
        assert set(affinity) <= {"nodeAffinity", "podAffinity", "podAntiAffinity"}
        terms = affinity["nodeAffinity"][
            "requiredDuringSchedulingIgnoredDuringExecution"
        ]["nodeSelectorTerms"]
        for term in terms:
            for expr in term["matchExpressions"]:
                assert set(expr) == {"key", "operator", "values"}, values_file
                if expr["operator"] in ("Gt", "Lt"):
                    assert len(expr["values"]) == 1 and expr["values"][0].isdigit()
        checked += 1
    assert checked, "no release carries interlink.eu/remote-affinity"


def test_probes_on_slurm_nodes_use_port_numbers(slurm_clusters):
    """The Slurm plugin runs a pod's probes in the job and passes a named port
    through as 0, so an HTTP probe on "http" never succeeds and the startup
    probe kills the container. The supersonic chart's schema requires a string
    and renders it unquoted, so the number goes in quotes."""
    nodes = {app["values"]["nodeName"] for app in slurm_clusters.values()}
    checked = 0
    for values_file in sorted((REPO / "apps" / "sonic").glob("*/values.yaml")):
        server = (yaml.safe_load(values_file.read_text()) or {}).get(
            "inferenceServer", {}
        )
        if (server.get("nodeSelector") or {}).get(
            "kubernetes.io/hostname"
        ) not in nodes:
            continue
        for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
            for action in ("httpGet", "tcpSocket", "grpc"):
                port = (server.get(probe) or {}).get(action, {}).get("port")
                assert port is None or (isinstance(port, str) and port.isdigit()), (
                    f"{values_file}: {probe}.{action}.port={port!r}"
                )
        checked += 1
    assert checked, "no SuperSONIC release runs on a Slurm node"


def test_kubernetes_nodes_have_no_full_mesh(kubernetes_clusters):
    """Full mesh runs privileged, NET_ADMIN pods on both clusters, which the
    Geddes admission policies refuse."""
    for cluster, app in kubernetes_clusters.items():
        network = app["values"]["virtualNode"].get("network", {})
        assert not network.get("fullMesh"), f"{cluster}: full mesh is configured"


# --- credentials stay out of git --------------------------------------------


def test_pvcs_are_not_declared_in_git(interlink_clusters):
    """Out-of-band volumes (the munge PVCs) are created by hand; a git-declared
    PVC would collide with the existing bound one, whose spec is immutable.
    Manifests for the remote cluster (`to-deploy-at-nrp/`) are not Geddes'."""
    for path in INTERLINK.rglob("*.yaml"):
        if "to-deploy-at-nrp" in path.parts:
            continue
        for doc in yaml.safe_load_all(path.read_text()):
            if isinstance(doc, dict) and doc.get("kind") == "PersistentVolumeClaim":
                pytest.fail(f"{path}: out-of-band PVC declared in git")


def test_credentials_are_never_committed():
    """The munge key is mounted from a PVC, the kubeconfig from a Secret
    created out of band; a committed Secret is SOPS-encrypted
    (test_secrets.py checks it). Any other Secret here would mean a
    credential leaked into git."""
    for path in INTERLINK.rglob("*.yaml"):
        for doc in yaml.safe_load_all(path.read_text()):
            if (
                isinstance(doc, dict)
                and doc.get("kind") == "Secret"
                and "sops" not in doc
            ):
                pytest.fail(f"{path}: Secret committed in plain text")


# --- values that Flux rewrites -------------------------------------------


def test_only_namespace_is_substituted(interlink_clusters):
    """Flux postBuild substitutes every `${VAR}` in the rendered stream, and an
    unset one becomes an empty string. Bare `$VAR` (the wstunnel command) is
    passed through untouched, so it needs no escaping."""
    for cluster, app in interlink_clusters.items():
        text = (app["dir"] / "values.yaml").read_text()
        found = set(re.findall(r"\$\{([^}]*)\}", text))
        assert found <= {"namespace"}, f"{cluster}: {found - {'namespace'}}"


def test_plugin_envs_are_well_formed(interlink_clusters):
    """`values:` instead of `value:` reaches the container as an empty variable."""
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
